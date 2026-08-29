import { EntitySchema } from 'typeorm'

export interface EditableWorkflowField {
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
}

// doc/03 §3 WorkflowConfig：workflows/*.json 在库中的镜像。
// 文件仍是真相，经 services/workflows.ts 单向同步（POST /api/workflows/sync
// 或 POST /api/tasks 的 sync-on-demand）。
export interface WorkflowConfig {
  id: number
  /** 文件名去扩展名，如 minimax_h3 */
  slug: string
  name: string
  /** 相对仓库根，如 workflows/minimax_h3.json */
  configPath: string
  /** 解析 api_json 后的实际路径（相对仓库根） */
  apiJsonPath: string
  supportedAccelerators: string[]
  acceleratorNote: string | null
  inputFields: { class_type: string, field: string, local_dir: string }[]
  editableFields: EditableWorkflowField[]
  comfyFlags: string[]
  /** 原样存，含 size_bytes */
  models: Record<string, unknown>[]
  /** 派生：models.size_bytes 之和；bigint 由 mysql2 返回 string */
  totalModelBytes: string
  syncedAt: Date
}

export const WorkflowConfigSchema = new EntitySchema<WorkflowConfig>({
  name: 'WorkflowConfig',
  tableName: 'workflow_configs',
  columns: {
    id: { type: 'int', primary: true, generated: 'increment' },
    slug: { type: 'varchar', length: 128, unique: true },
    name: { type: 'varchar', length: 255 },
    configPath: { type: 'varchar', length: 512 },
    apiJsonPath: { type: 'varchar', length: 512 },
    supportedAccelerators: { type: 'json' },
    acceleratorNote: { type: 'text', nullable: true },
    inputFields: { type: 'json' },
    editableFields: { type: 'json' },
    comfyFlags: { type: 'json' },
    models: { type: 'json' },
    totalModelBytes: { type: 'bigint' },
    syncedAt: { type: 'datetime' },
  },
})
