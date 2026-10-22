import { defineStore } from 'pinia'

// doc/04 §3：task 任务列表、分页、筛选、提交。

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'recoverable'
  | 'canceled'
  | 'orphaned'

export type CancelMode = 'interrupt' | 'destroy'
export type FtpUploadStatus = 'pending' | 'uploading' | 'uploaded' | 'failed'

/** GET /api/tasks 返回的任务行（doc/03 §3；datetime 序列化为 ISO 串，bigint 为 string） */
export interface TaskRow {
  id: string
  workflowConfigId: number
  workflowName: string
  sessionName: string
  /** 计算后端 colab | kaggle | modal（迁移 013 之前的行为 colab） */
  backend?: string
  acceleratorKind: 'gpu' | 'tpu'
  acceleratorSpec: string
  mountDrive: boolean
  reuse: boolean
  keep: boolean
  timeout: number
  status: TaskStatus
  currentPhase: string | null
  queuedAt: string
  startedAt: string | null
  finishedAt: string | null
  durationMs: string | null
  exitCode: number | null
  lastSeq: number
  cancelMode: CancelMode | null
  outputZipPath: string | null
  outputSizeBytes: string | null
  outputSha256: string | null
  ftpStatus: FtpUploadStatus | null
  ftpRemotePath: string | null
  ftpUploadedAt: string | null
  ftpError: string | null
  ftpAttempts: number
  keepOutput: boolean
  outputDownloadedAt: string | null
  logPath: string | null
  sessionKept: boolean
  transportMode: 'native' | 'cli' | 'dry_run' | null
  errorCode: string | null
  errorMessage: string | null
  createdByUserId: number | null
}

/** POST /api/tasks 请求体（doc/02 §2.3，字段与 run.py argparse 一一对应） */
export interface SubmitTaskPayload {
  workflow_config: string
  api_json: string | null
  session: string
  accelerator: { kind: 'gpu' | 'tpu', spec: string }
  /** 计算后端，缺省 colab */
  backend?: 'colab' | 'kaggle' | 'modal'
  mount_drive: boolean
  reuse: boolean
  keep: boolean
  input_dir: string | null
  /** 工作流 editable_fields 白名单内的任务级 API 节点输入覆盖。 */
  workflow_overrides: Record<string, string>
  /** 秒 */
  timeout: number
  /** Runner 演练通道（不烧计算单元），仅调试 */
  dry_run?: boolean
}

/** GET /api/tasks/:id 附带的产物保留信息（server/services/outputRetention.ts） */
export interface TaskRetention {
  reason: 'not_terminal' | 'keep' | 'unsynced' | 'standard' | 'disabled'
  expiresAt: string | null
  retentionDays: number | null
}

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '已成功',
  failed: '失败',
  recoverable: '可重取（VM 存活）',
  canceled: '已取消',
  orphaned: '已失联',
}

export const TERMINAL_STATUSES: TaskStatus[] = [
  'succeeded',
  'failed',
  'recoverable',
  'canceled',
  'orphaned',
]

export function isTerminalStatus(status: TaskStatus): boolean {
  return TERMINAL_STATUSES.includes(status)
}

export const useTaskStore = defineStore('task', () => {
  const tasks = ref<TaskRow[]>([])
  const loading = ref(false)
  const page = ref(1)
  const size = ref(20)
  const total = ref(0)
  /** 空串 = 全部 */
  const statusFilter = ref('')

  const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

  async function fetchTasks(): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    loading.value = true
    try {
      const res = await $apiFetch<{ items: TaskRow[], total: number, page: number, size: number }>(
        '/api/tasks',
        { query: { status: statusFilter.value, page: page.value, size: size.value } },
      )
      tasks.value = res.items
      total.value = res.total
    } finally {
      loading.value = false
    }
  }

  function setStatusFilter(status: string) {
    statusFilter.value = status
    page.value = 1
  }

  /** POST /api/tasks（operator+）；成功返回 Runner 生成的 task_id */
  async function submitTask(payload: SubmitTaskPayload): Promise<string> {
    const { $apiFetch } = useNuxtApp()
    const res = await $apiFetch<{ task_id: string, status: string }>('/api/tasks', {
      method: 'POST',
      body: payload,
    })
    return res.task_id
  }

  /** POST /api/tasks/:id/cancel（doc/02 §2.3.2：interrupt 默认 / destroy 产物丢失） */
  async function cancelTask(id: string, mode: CancelMode): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    await $apiFetch(`/api/tasks/${id}/cancel`, { method: 'POST', body: { mode } })
  }

  /** POST /api/tasks/:id/redownload（recoverable → 重取结果，doc/03 §4） */
  async function redownloadTask(id: string): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    await $apiFetch(`/api/tasks/${id}/redownload`, { method: 'POST' })
  }

  /** 浏览器完成 Drive OAuth 后，通知 Runner 继续挂载。 */
  async function confirmDrive(id: string): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    await $apiFetch(`/api/tasks/${id}/drive-confirm`, { method: 'POST' })
  }

  async function fetchTask(id: string): Promise<TaskRow> {
    return (await fetchTaskDetail(id)).task
  }

  async function fetchTaskDetail(id: string): Promise<{ task: TaskRow, retention: TaskRetention | null }> {
    const { $apiFetch } = useNuxtApp()
    const res = await $apiFetch<{ task: TaskRow, retention?: TaskRetention }>(`/api/tasks/${id}`)
    return { task: res.task, retention: res.retention ?? null }
  }

  async function setKeepOutput(id: string, keep: boolean): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    await $apiFetch(`/api/tasks/${id}/keep`, { method: 'PATCH', body: { keep } })
  }

  return {
    tasks,
    loading,
    page,
    size,
    total,
    totalPages,
    statusFilter,
    fetchTasks,
    setStatusFilter,
    submitTask,
    cancelTask,
    redownloadTask,
    confirmDrive,
    fetchTask,
    fetchTaskDetail,
    setKeepOutput,
  }
})
