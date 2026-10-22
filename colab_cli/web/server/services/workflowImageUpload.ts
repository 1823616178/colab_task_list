import { randomUUID } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'

const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff'])
const imageWriteQueues = new Map<string, Promise<void>>()

function isPathInside(base: string, candidate: string): boolean {
  const relative = path.relative(base, candidate)
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))
}

export async function updateWorkflowInputImage(
  root: string,
  slug: string,
  nodeId: string,
  filename: string,
  data: Buffer,
): Promise<'updated' | 'not_found' | 'unsupported'> {
  const configPath = path.join(root, 'workflows', `${slug}.json`)
  const extension = path.extname(filename).toLowerCase()
  if (!IMAGE_EXTENSIONS.has(extension)) return 'unsupported'

  const key = path.resolve(configPath)
  const previous = imageWriteQueues.get(key) ?? Promise.resolve()
  let release!: () => void
  const current = new Promise<void>((resolve) => { release = resolve })
  imageWriteQueues.set(key, current)
  await previous
  try {
    let config: Record<string, unknown>
    let api: Record<string, unknown>
    let apiPath: string
    try {
      config = JSON.parse(await fs.readFile(configPath, 'utf8')) as Record<string, unknown>
      apiPath = typeof config.api_json === 'string'
        ? path.resolve(path.dirname(configPath), config.api_json)
        : path.join(root, 'api.json')
      api = JSON.parse(await fs.readFile(apiPath, 'utf8')) as Record<string, unknown>
    } catch {
      return 'not_found'
    }
    const node = api[nodeId]
    if (typeof node !== 'object' || node === null) return 'not_found'
    const record = node as Record<string, unknown>
    if (record.class_type !== 'LoadImage') return 'not_found'
    const inputs = record.inputs
    if (typeof inputs !== 'object' || inputs === null) return 'not_found'
    const rules = Array.isArray(config.input_fields) ? config.input_fields : []
    const rule = rules.find((candidate: Record<string, unknown>) =>
      candidate.class_type === 'LoadImage' && candidate.field === 'image') as Record<string, unknown> | undefined
    if (!rule) return 'not_found'

    const directory = typeof rule.local_dir === 'string'
      ? path.resolve(path.dirname(configPath), rule.local_dir)
      : path.join(root, 'image')
    if (!isPathInside(root, directory)) return 'not_found'
    await fs.mkdir(directory, { recursive: true })
    const imageName = `${randomUUID()}${extension}`
    const imagePath = path.join(directory, imageName)
    await fs.writeFile(imagePath, data, { flag: 'wx' })
    const tempPath = `${apiPath}.${randomUUID()}.tmp`
    try {
      (inputs as Record<string, unknown>).image = imageName
      await fs.writeFile(tempPath, `${JSON.stringify(api, null, 2)}\n`)
      await fs.rename(tempPath, apiPath)
    } catch (error) {
      await Promise.all([
        fs.rm(tempPath, { force: true }),
        fs.rm(imagePath, { force: true }),
      ])
      throw error
    }
    return 'updated'
  } finally {
    release()
    if (imageWriteQueues.get(key) === current) imageWriteQueues.delete(key)
  }
}
