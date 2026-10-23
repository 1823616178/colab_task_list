import { defineStore } from 'pinia'

// doc/04 §3：workflow 工作流列表与同步。
// 提交任务页用 supportedAccelerators 做加速器置灰 + acceleratorNote 提示（doc/04 §2.1）。

/** GET /api/workflows 返回的工作流（workflow_configs 表镜像，doc/03 §2） */
export interface WorkflowItem {
  id: number
  slug: string
  name: string
  /** 相对仓库根，如 workflows/minimax_h3.json —— 提交任务时作 workflow_config */
  configPath: string
  apiJsonPath: string
  supportedAccelerators: string[]
  acceleratorNote: string | null
  inputFields: { class_type: string, field: string, local_dir: string }[]
  inputImages: WorkflowInputImage[]
  optionalLastFrame?: boolean
  editableFields: EditableWorkflowField[]
  comfyFlags: string[]
  models: Record<string, unknown>[]
  /** bigint 由 mysql2 返回 string */
  totalModelBytes: string
  syncedAt: string
}

export interface WorkflowInputImage {
  nodeId: string
  label: string
  filename: string
  available: boolean
  url: string | null
}

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
  defaultValue?: string
}

/** GET /api/models 返回的模型条目（workflow 声明 ∪ model_cache 实况，doc/05 P5） */
export interface ModelItem {
  /** null = 无法从 HuggingFace 获取，须手工放置到 Drive（Wan2.2-big 特例，doc/03 §3） */
  repo: string | null
  filename: string
  comfyDir: string
  cachePath: string | null
  sizeBytes: string
  present: boolean
  lastSource: 'drive_cache' | 'huggingface' | 'vm_local' | null
  lastVerifiedAt: string | null
  /** 声明依赖该模型的工作流 slug；空数组 = 仅 model_cache 残留 */
  workflows: string[]
  manual: boolean
}

export const useWorkflowStore = defineStore('workflow', () => {
  const workflows = ref<WorkflowItem[]>([])
  const loading = ref(false)
  const syncing = ref(false)
  const models = ref<ModelItem[]>([])
  const modelsLoading = ref(false)

  async function fetchWorkflows(): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    loading.value = true
    try {
      const res = await $apiFetch<{ items: WorkflowItem[] }>('/api/workflows')
      workflows.value = res.items
    } finally {
      loading.value = false
    }
  }

  /** POST /api/workflows/sync：workflows/*.json → workflow_configs 表（operator+） */
  async function sync(): Promise<{ synced: number, slugs: string[] }> {
    const { $apiFetch } = useNuxtApp()
    syncing.value = true
    try {
      const res = await $apiFetch<{ synced: number, slugs: string[] }>('/api/workflows/sync', {
        method: 'POST',
      })
      await fetchWorkflows()
      return res
    } finally {
      syncing.value = false
    }
  }

  /** GET /api/models：模型清单（可选按工作流 slug 过滤） */
  async function fetchModels(workflowSlug?: string): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    modelsLoading.value = true
    try {
      const query = workflowSlug ? `?workflow=${encodeURIComponent(workflowSlug)}` : ''
      const res = await $apiFetch<{ items: ModelItem[] }>(`/api/models${query}`)
      models.value = res.items
    } finally {
      modelsLoading.value = false
    }
  }

  return { workflows, loading, syncing, models, modelsLoading, fetchWorkflows, fetchModels, sync }
})
