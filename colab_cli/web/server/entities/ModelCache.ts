import { EntitySchema } from 'typeorm'

// doc/03 §3 ModelCache：Drive 模型缓存可见性。由 model.prepare 事件的 data 更新
// （services/taskEvents.ts，含 progress 事件 —— 不入库 task_events 但可用于对账）。
export type ModelSource = 'drive_cache' | 'huggingface' | 'vm_local'

export interface ModelCache {
  id: number
  /** 如 Comfy-Org/MiniMax-H3 */
  repo: string
  /** 仓库内路径 */
  filename: string
  /** diffusion_models / vae / loras …（事件无此字段，取 filename 首段） */
  comfyDir: string
  /** Drive 内相对路径（事件无此字段，取 filename） */
  cachePath: string
  /** bigint → mysql2 返回 string */
  sizeBytes: string
  /** 上次任务中是否命中 */
  present: boolean
  lastSource: ModelSource
  lastVerifiedAt: Date
}

export const ModelCacheSchema = new EntitySchema<ModelCache>({
  name: 'ModelCache',
  tableName: 'model_cache',
  columns: {
    id: { type: 'int', primary: true, generated: 'increment' },
    repo: { type: 'varchar', length: 255 },
    filename: { type: 'varchar', length: 512 },
    comfyDir: { type: 'varchar', length: 64 },
    cachePath: { type: 'varchar', length: 512 },
    sizeBytes: { type: 'bigint' },
    present: { type: 'boolean', default: false },
    lastSource: { type: 'enum', enum: ['drive_cache', 'huggingface', 'vm_local'] },
    lastVerifiedAt: { type: 'datetime' },
  },
  indices: [
    { name: 'UQ_model_cache_repo_filename', columns: ['repo', 'filename'], unique: true },
  ],
})
