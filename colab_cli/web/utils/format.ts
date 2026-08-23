// 展示层格式化约定（doc/04 §4）：
// - 时间：库内与接口全部 UTC ISO 串，展示时转本地时区；task_id 前缀是 UTC，不当本地时间显示
// - 体积：模型/输出用 GiB/MiB（与 remote_workflow.py size_bytes 口径一致）

/** UTC ISO 串 → 本地时间；空值显示占位 */
export function formatLocalTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
}

/** 字节 → GiB/MiB 可读串（二进制口径） */
export function formatBytes(value: string | number | null | undefined): string {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GiB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${bytes} B`
}

/** 毫秒时长 → 可读串（如 1 小时 23 分钟） */
export function formatDurationMs(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const ms = Number(value)
  if (!Number.isFinite(ms) || ms < 0) return '—'
  const totalSeconds = Math.round(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours} 小时 ${minutes} 分钟`
  if (minutes > 0) return `${minutes} 分 ${seconds} 秒`
  return `${seconds} 秒`
}

/** 两个 UTC 时间之间的时长 */
export function formatDurationBetween(start: string | null, end: string | null): string {
  if (!start) return '—'
  const startMs = new Date(start).getTime()
  const endMs = end ? new Date(end).getTime() : Date.now()
  if (Number.isNaN(startMs) || Number.isNaN(endMs) || endMs < startMs) return '—'
  return formatDurationMs(endMs - startMs)
}

/** phase → 中文标签（doc/02 §1.2） */
export const PHASE_LABELS: Record<string, string> = {
  'task.queued': '排队',
  'task.start': '任务开始',
  'session.create': '新建会话',
  'session.reuse': '复用会话',
  'drive.mount': '挂载 Drive',
  'input.resolve': '解析输入',
  'input.upload': '上传素材',
  'env.setup': '环境准备',
  'model.prepare': '模型准备',
  'node.prepare': '自定义节点',
  'comfy.start': '启动 ComfyUI',
  'workflow.exec': '工作流执行',
  'output.pack': '打包产物',
  'output.transfer': '传输产物',
  'session.stop': '销毁会话',
  'session.keep': '保留会话',
  'task.done': '任务成功',
  'task.failed': '任务失败',
  'task.canceled': '任务取消',
}

export function formatPhase(phase: string): string {
  return PHASE_LABELS[phase] ?? phase
}
