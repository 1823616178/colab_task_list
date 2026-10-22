// WorkflowConfig 同步：workflows/*.json → workflow_configs 表（doc/03 §3）。
// 文件仍是真相；两个入口：
// - POST /api/workflows/sync（operator+，全量强制）
// - POST /api/tasks 的 sync-on-demand：行缺失或文件 mtime 晚于 syncedAt 时同步，
//   避免首次使用时 workflow_configs 为空导致 FK 失败（doc/05 P3）。
import { promises as fs } from 'node:fs'
import path from 'node:path'
import type { WorkflowConfig } from '../entities/WorkflowConfig'
import { WorkflowConfigSchema } from '../entities/WorkflowConfig'
import { getDataSource } from '../utils/db'

export function repoRoot(): string {
  const config = useRuntimeConfig()
  // 缺省回退 <cwd>/..（dev 下 cwd=web/）；生产部署必须显式配 NUXT_REPO_ROOT
  return path.resolve(String(config.repoRoot || '') || path.join(process.cwd(), '..'))
}

export function workflowsDir(): string {
  return path.join(repoRoot(), 'workflows')
}

export interface EditableWorkflowFieldView {
  key: string
  label: string
  class_type?: string
  node_id?: string
  field: string
  control: 'text' | 'textarea' | 'select'
  value_type?: 'string' | 'number'
  options?: { label: string, value: string }[]
  rows?: number
  help?: string
  required?: boolean
  defaultValue?: string
}

export interface WorkflowInputImageView {
  nodeId: string
  label: string
  filename: string
  available: boolean
  url: string | null
}

export interface ResolvedWorkflowInputImage {
  path: string
  filename: string
  contentType: string
}

const IMAGE_CONTENT_TYPES: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
  '.gif': 'image/gif',
  '.tif': 'image/tiff',
  '.tiff': 'image/tiff',
}

interface RawWorkflowConfig {
  name?: string
  api_json?: string
  supported_accelerators?: string[]
  accelerator_note?: string
  input_fields?: Record<string, unknown>[]
  editable_fields?: Record<string, unknown>[]
  comfy_flags?: string[]
  models?: Record<string, unknown>[]
}

export function slugOfConfigFile(filename: string): string {
  return filename.replace(/\.json$/i, '')
}

function configFilePath(slug: string): string {
  return path.join(workflowsDir(), `${slug}.json`)
}

function apiFilePath(configPath: string, config: RawWorkflowConfig): string {
  return config.api_json
    ? path.resolve(path.dirname(configPath), config.api_json)
    : path.join(repoRoot(), 'api.json')
}

function isPathInside(base: string, candidate: string): boolean {
  const relative = path.relative(base, candidate)
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))
}

async function resolveInputReference(
  reference: string,
  localDir: unknown,
  configPath: string,
  apiPath: string,
): Promise<string | null> {
  const configuredDirectory = typeof localDir === 'string'
    ? path.resolve(path.dirname(configPath), localDir)
    : null
  const directories = configuredDirectory
    ? [configuredDirectory]
    : [
        path.join(repoRoot(), 'image'),
        path.join(path.dirname(apiPath), 'image'),
        path.dirname(apiPath),
        repoRoot(),
      ]
  for (const directory of directories) {
    const base = path.resolve(directory)
    const candidate = path.resolve(base, reference)
    if (!isPathInside(base, candidate)) continue
    const stat = await fs.stat(candidate).catch(() => null)
    if (stat?.isFile()) return candidate
  }
  return null
}

function frameLabels(api: Record<string, unknown>): Map<string, string> {
  const labels = new Map<string, string>()
  const imageTransforms = new Set(['ImageScaleToTotalPixels', 'ImageScale', 'ImageScaleBy'])
  for (const node of Object.values(api)) {
    if (typeof node !== 'object' || node === null) continue
    const record = node as Record<string, unknown>
    if (record.class_type !== 'MiniMaxH3ImageToVideo') continue
    const inputs = record.inputs
    if (typeof inputs !== 'object' || inputs === null) continue
    for (const [field, label] of [['first_frame', '首帧'], ['last_frame', '尾帧']] as const) {
      const connection = (inputs as Record<string, unknown>)[field]
      if (Array.isArray(connection) && typeof connection[0] === 'string') {
        let nodeId: string = connection[0]
        const visited = new Set<string>()
        while (!visited.has(nodeId)) {
          visited.add(nodeId)
          const upstream = api[nodeId]
          if (typeof upstream !== 'object' || upstream === null) break
          const imageNode = upstream as Record<string, unknown>
          if (imageNode.class_type === 'LoadImage') {
            labels.set(nodeId, label)
            break
          }
          if (!imageTransforms.has(String(imageNode.class_type))) break
          const imageInput = (imageNode.inputs as Record<string, unknown> | undefined)?.image
          if (!Array.isArray(imageInput) || typeof imageInput[0] !== 'string') break
          nodeId = imageInput[0]
        }
      }
    }
  }
  return labels
}

async function readWorkflowFiles(slug: string): Promise<{
  configPath: string
  config: RawWorkflowConfig
  apiPath: string
  api: Record<string, unknown>
} | null> {
  const configPath = configFilePath(slug)
  try {
    const config = JSON.parse(await fs.readFile(configPath, 'utf-8')) as RawWorkflowConfig
    const apiPath = apiFilePath(configPath, config)
    const api = JSON.parse(await fs.readFile(apiPath, 'utf-8')) as Record<string, unknown>
    return { configPath, config, apiPath, api }
  } catch {
    return null
  }
}

export async function resolveWorkflowInputImage(
  slug: string,
  nodeId: string,
): Promise<ResolvedWorkflowInputImage | null> {
  const files = await readWorkflowFiles(slug)
  if (!files) return null
  const node = files.api[nodeId]
  if (typeof node !== 'object' || node === null) return null
  const record = node as Record<string, unknown>
  const inputs = record.inputs
  if (typeof inputs !== 'object' || inputs === null) return null

  const rules = Array.isArray(files.config.input_fields) ? files.config.input_fields : []
  const rule = rules.find(candidate =>
    candidate.class_type === record.class_type
    && typeof candidate.field === 'string'
    && typeof (inputs as Record<string, unknown>)[candidate.field] === 'string',
  )
  if (!rule || typeof rule.field !== 'string') return null
  const filename = (inputs as Record<string, unknown>)[rule.field]
  if (typeof filename !== 'string') return null
  const extension = path.extname(filename).toLowerCase()
  const contentType = IMAGE_CONTENT_TYPES[extension]
  if (!contentType) return null
  const resolvedPath = await resolveInputReference(
    filename,
    rule.local_dir,
    files.configPath,
    files.apiPath,
  )
  return resolvedPath ? { path: resolvedPath, filename, contentType } : null
}

async function buildRow(slug: string, filePath: string): Promise<Omit<WorkflowConfig, 'id'>> {
  const raw: RawWorkflowConfig = JSON.parse(await fs.readFile(filePath, 'utf-8'))
  const root = repoRoot()
  const apiJsonAbs = raw.api_json
    ? path.resolve(path.dirname(filePath), raw.api_json)
    : path.join(root, 'api.json')
  const models = Array.isArray(raw.models) ? raw.models : []
  const totalModelBytes = models.reduce(
    (sum, model) => sum + (typeof model?.size_bytes === 'number' ? model.size_bytes : 0),
    0,
  )
  return {
    slug,
    name: String(raw.name ?? slug),
    configPath: path.relative(root, filePath),
    apiJsonPath: path.relative(root, apiJsonAbs),
    supportedAccelerators: Array.isArray(raw.supported_accelerators) ? raw.supported_accelerators : [],
    acceleratorNote: raw.accelerator_note ?? null,
    inputFields: (Array.isArray(raw.input_fields) ? raw.input_fields : []) as WorkflowConfig['inputFields'],
    editableFields: (Array.isArray(raw.editable_fields)
      ? raw.editable_fields
      : []) as unknown as WorkflowConfig['editableFields'],
    comfyFlags: Array.isArray(raw.comfy_flags) ? raw.comfy_flags : [],
    models,
    totalModelBytes: String(totalModelBytes),
    syncedAt: new Date(),
  }
}

/** 全量同步（文件 → 表，按 slug upsert；文件消失的 slug 不删，FK 可能仍被引用）。 */
export async function syncWorkflowConfigs(): Promise<WorkflowConfig[]> {
  const dir = workflowsDir()
  const files = (await fs.readdir(dir)).filter(name => name.endsWith('.json')).sort()
  const ds = await getDataSource()
  const repo = ds.getRepository(WorkflowConfigSchema)
  for (const file of files) {
    const slug = slugOfConfigFile(file)
    try {
      const row = await buildRow(slug, path.join(dir, file))
      const existing = await repo.findOne({ where: { slug } })
      await repo.save(existing ? { ...row, id: existing.id } : row)
    } catch (error) {
      console.warn(`[workflows] 同步 ${file} 失败，已跳过:`, error)
    }
  }
  return await repo.find({ order: { slug: 'ASC' } })
}

/**
 * sync-on-demand（POST /api/tasks 用）：行不存在，或文件 mtime 晚于 syncedAt
 * （文件被改过）→ 先全量同步。返回该 slug 的最新行；文件缺失返回 null。
 */
export async function ensureWorkflowConfig(slug: string): Promise<WorkflowConfig | null> {
  const filePath = configFilePath(slug)
  let mtime: Date
  try {
    mtime = (await fs.stat(filePath)).mtime
  } catch {
    return null // 文件不存在：INPUT_NOT_FOUND 语义由调用方按 400 处理
  }
  const ds = await getDataSource()
  const repo = ds.getRepository(WorkflowConfigSchema)
  const existing = await repo.findOne({ where: { slug } })
  if (!existing || mtime.getTime() > new Date(existing.syncedAt).getTime()) {
    await syncWorkflowConfigs()
  }
  return await repo.findOne({ where: { slug } })
}

/** 从当前 api.json 读取可编辑字段默认值，供任务表单预填；读取失败时仍返回声明。 */
export async function hydrateEditableFieldDefaults(
  workflow: WorkflowConfig,
): Promise<WorkflowConfig & {
  editableFields: EditableWorkflowFieldView[]
  inputImages: WorkflowInputImageView[]
}> {
  let fields = workflow.editableFields as EditableWorkflowFieldView[]
  // GET 不能假设操作员已经手工点过「从文件同步」；直接读取文件声明，
  // POST /tasks 随后仍会通过 ensureWorkflowConfig 把同一声明同步入库。
  try {
    const configPath = path.resolve(repoRoot(), workflow.configPath)
    const raw = JSON.parse(await fs.readFile(configPath, 'utf-8')) as RawWorkflowConfig
    if (Array.isArray(raw.editable_fields)) {
      fields = raw.editable_fields as unknown as EditableWorkflowFieldView[]
    }
  } catch {
    // 配置暂时不可读时退回数据库镜像，不拖垮整个工作流列表。
  }
  try {
    const configPath = path.resolve(repoRoot(), workflow.configPath)
    const raw = JSON.parse(await fs.readFile(configPath, 'utf-8')) as RawWorkflowConfig
    const apiPath = apiFilePath(configPath, raw)
    const api = JSON.parse(await fs.readFile(apiPath, 'utf-8')) as Record<string, unknown>
    const hydrated = fields.map((field) => {
      const candidates = field.node_id
        ? [[field.node_id, api[field.node_id]] as const]
        : Object.entries(api).filter(([, candidate]) => {
            return typeof candidate === 'object'
              && candidate !== null
              && (candidate as Record<string, unknown>).class_type === field.class_type
          })
      if (candidates.length !== 1) return field
      const candidate = candidates[0]![1]
      if (typeof candidate !== 'object' || candidate === null) return field
      const inputs = (candidate as Record<string, unknown>).inputs
      if (typeof inputs !== 'object' || inputs === null) return field
      const value = (inputs as Record<string, unknown>)[field.field]
      return typeof value === 'string' || typeof value === 'number'
        ? { ...field, defaultValue: String(value) }
        : field
    })
    const labels = frameLabels(api)
    const inputImages: WorkflowInputImageView[] = []
    const seenNodes = new Set<string>()
    const rules = Array.isArray(raw.input_fields) ? raw.input_fields : []
    for (const rule of rules) {
      if (rule.class_type !== 'LoadImage' || typeof rule.field !== 'string') continue
      for (const [nodeId, candidate] of Object.entries(api)) {
        if (seenNodes.has(nodeId) || typeof candidate !== 'object' || candidate === null) continue
        const node = candidate as Record<string, unknown>
        if (node.class_type !== rule.class_type) continue
        const inputs = node.inputs
        if (typeof inputs !== 'object' || inputs === null) continue
        const filename = (inputs as Record<string, unknown>)[rule.field]
        if (typeof filename !== 'string' || !IMAGE_CONTENT_TYPES[path.extname(filename).toLowerCase()]) continue
        const resolvedPath = await resolveInputReference(filename, rule.local_dir, configPath, apiPath)
        inputImages.push({
          nodeId,
          label: labels.get(nodeId) ?? '输入图片',
          filename,
          available: resolvedPath !== null,
          url: resolvedPath
            ? `/api/workflows/${encodeURIComponent(workflow.slug)}/images/${encodeURIComponent(nodeId)}`
            : null,
        })
        seenNodes.add(nodeId)
      }
    }
    return { ...workflow, editableFields: hydrated, inputImages }
  } catch {
    return { ...workflow, editableFields: fields, inputImages: [] }
  }
}
