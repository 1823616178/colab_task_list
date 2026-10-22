// 产物保留期计算（纯函数，无 DB 依赖，便于单测与前端提示共用）。
// 规则（优先级从高到低）：
// 1. 非终态 / 无 finishedAt → 不清理；
// 2. keepOutput=true → 永久保留；
// 3. 成功的真实产物「既未在控制台下载过、FTP 也未镜像成功」→ 延长保留期
//    （unsyncedRetentionDays），避免唯一副本被定时清理删掉；
// 4. 其余 → 标准保留期（retentionDays）。

export type RetentionReason = 'not_terminal' | 'keep' | 'unsynced' | 'standard'

export interface RetentionTaskView {
  status: string
  finishedAt: Date | string | null
  keepOutput: boolean
  ftpStatus: string | null
  outputZipPath: string | null
  outputDownloadedAt: Date | string | null
  transportMode: string | null
}

export interface RetentionOptions {
  retentionDays: number
  unsyncedRetentionDays: number
}

export interface RetentionInfo {
  reason: RetentionReason
  /** 预计清理时间；null = 不会被自动清理 */
  expiresAt: Date | null
  retentionDays: number | null
}

const TERMINAL = new Set(['succeeded', 'failed', 'recoverable', 'canceled', 'orphaned'])
const DAY_MS = 24 * 60 * 60 * 1000

export function isUnsyncedOutput(task: RetentionTaskView): boolean {
  return task.status === 'succeeded'
    && Boolean(task.outputZipPath)
    && task.transportMode !== 'dry_run'
    && task.ftpStatus !== 'uploaded'
    && !task.outputDownloadedAt
}

export function retentionFor(task: RetentionTaskView, options: RetentionOptions): RetentionInfo {
  if (!TERMINAL.has(task.status) || !task.finishedAt) {
    return { reason: 'not_terminal', expiresAt: null, retentionDays: null }
  }
  if (task.keepOutput) return { reason: 'keep', expiresAt: null, retentionDays: null }
  const finished = new Date(task.finishedAt).getTime()
  const unsynced = isUnsyncedOutput(task)
  const days = unsynced
    ? Math.max(options.unsyncedRetentionDays, options.retentionDays)
    : options.retentionDays
  return {
    reason: unsynced ? 'unsynced' : 'standard',
    expiresAt: new Date(finished + days * DAY_MS),
    retentionDays: days,
  }
}

export function shouldDeleteOutput(
  task: RetentionTaskView,
  options: RetentionOptions,
  now: Date = new Date(),
): boolean {
  const info = retentionFor(task, options)
  return info.expiresAt !== null && info.expiresAt.getTime() <= now.getTime()
}

export function retentionOptionsFromRuntime(config: {
  outputRetentionDays?: unknown
  outputUnsyncedRetentionDays?: unknown
}): RetentionOptions {
  const retentionDays = Number(config.outputRetentionDays)
  const unsynced = Number(config.outputUnsyncedRetentionDays)
  return {
    retentionDays,
    unsyncedRetentionDays: Number.isFinite(unsynced) && unsynced > 0 ? unsynced : retentionDays * 4,
  }
}
