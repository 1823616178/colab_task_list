// GET /api/models?workflow=<slug>（viewer+，doc/05 P5）：模型清单视图。
// 数据源两份合并：
// - workflow_configs.models（JSON 声明，snake_case 键）—— 每个工作流声明依赖哪些模型；
//   其中无 repo 的条目（如 Wan2.2-big.safetensors）是「无法从 HuggingFace 获取、
//   必须手工放置到 Drive」的特例（doc/03 §3 注释），标记 manual=true。
// - model_cache 表（model.prepare 事件驱动）—— 上次任务的命中情况
//   （present/lastSource/lastVerifiedAt）。
// model_cache 的 upsert 要求事件带 repo（services/taskEvents.ts），所以无 repo 的
// 条目永远不会有缓存行，present 恒为 false —— 这正是 UI 要给出手工指引的情形。
import { getQuery } from 'h3'
import { ModelCacheSchema } from '../../entities/ModelCache'
import type { ModelSource } from '../../entities/ModelCache'
import { WorkflowConfigSchema } from '../../entities/WorkflowConfig'
import { getDataSource } from '../../utils/db'

export interface ModelItem {
  /** null = 无法确认来源，须手工放置（Wan2.2-big.safetensors 特例） */
  repo: string | null
  filename: string
  comfyDir: string
  /** Drive 内相对路径（workflow 配置声明；model_cache 残留行取其实况值） */
  cachePath: string | null
  /** bigint 口径 string */
  sizeBytes: string
  /** 上次任务是否命中 Drive 缓存（无缓存行视为未命中） */
  present: boolean
  lastSource: ModelSource | null
  lastVerifiedAt: Date | null
  /** 声明依赖该模型的工作流 slug；空数组 = 仅 model_cache 残留 */
  workflows: string[]
  /** !repo：必须手工放置到 MyDrive/code/backup/models/<comfyDir>/ */
  manual: boolean
}

function declaredKey(repo: string | null, filename: string): string {
  return `${repo ?? ''}|${filename}`
}

export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const workflowSlug = typeof query.workflow === 'string' && query.workflow ? query.workflow : null

  const ds = await getDataSource()
  const workflows = await ds.getRepository(WorkflowConfigSchema).find({ order: { slug: 'ASC' } })
  const cacheRows = await ds.getRepository(ModelCacheSchema).find()

  // ── workflow_configs.models 声明（按 repo+filename 去重，workflows 归集）──
  const declared = new Map<string, ModelItem>()
  for (const workflow of workflows) {
    if (workflowSlug && workflow.slug !== workflowSlug) continue
    for (const model of workflow.models ?? []) {
      const filename = typeof model.filename === 'string' ? model.filename : ''
      if (!filename) continue
      const repo = typeof model.repo === 'string' && model.repo ? model.repo : null
      const key = declaredKey(repo, filename)
      let item = declared.get(key)
      if (!item) {
        item = {
          repo,
          filename,
          comfyDir: typeof model.comfy_dir === 'string' && model.comfy_dir
            ? model.comfy_dir
            : filename.split('/')[0] ?? '',
          cachePath: typeof model.cache_path === 'string' ? model.cache_path : null,
          sizeBytes: typeof model.size_bytes === 'number' ? String(model.size_bytes) : '0',
          present: false,
          lastSource: null,
          lastVerifiedAt: null,
          workflows: [],
          manual: !repo,
        }
        declared.set(key, item)
      }
      if (!item.workflows.includes(workflow.slug)) item.workflows.push(workflow.slug)
    }
  }

  // ── model_cache 实况：命中情况并回声明条目；未声明的残留行也列出 ──
  for (const cache of cacheRows) {
    const item = declared.get(declaredKey(cache.repo, cache.filename))
    if (item) {
      item.present = cache.present
      item.lastSource = cache.lastSource
      item.lastVerifiedAt = cache.lastVerifiedAt
    } else if (!workflowSlug) {
      declared.set(declaredKey(cache.repo, cache.filename), {
        repo: cache.repo,
        filename: cache.filename,
        comfyDir: cache.comfyDir,
        cachePath: cache.cachePath,
        sizeBytes: cache.sizeBytes,
        present: cache.present,
        lastSource: cache.lastSource,
        lastVerifiedAt: cache.lastVerifiedAt,
        workflows: [],
        manual: false,
      })
    }
  }

  const items = [...declared.values()].sort((a, b) => {
    if (a.manual !== b.manual) return a.manual ? -1 : 1 // 需手工放置的排最前
    return `${a.comfyDir}/${a.filename}`.localeCompare(`${b.comfyDir}/${b.filename}`)
  })
  return { items }
})
