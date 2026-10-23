/** Runner 的任务记录比一次云端会话列表查询更适合判断是否继续订阅。 */
export interface TrackedTask {
  id: string
  sessionName: string
}

export interface RunnerTaskSnapshot {
  task_id: string
  session: string
  status: string
  phase: string | null
  last_seq: number
}

export type TaskTracking =
  | { kind: 'tracked', snapshot: RunnerTaskSnapshot }
  | { kind: 'missing' }
  | { kind: 'interrupted' }
  | { kind: 'unavailable' }

const RUNNER_STATUSES = new Set(['queued', 'running', 'redownloading', 'succeeded', 'failed', 'canceled'])

/** 区分执行仍被接管、明确无记录、Runner 重启后线程已中断，以及查询失败。 */
export async function verifyTaskTracking(
  task: TrackedTask,
  fetchTask: (taskId: string) => Promise<RunnerTaskSnapshot>,
): Promise<TaskTracking> {
  try {
    const snapshot = await fetchTask(task.id)
    if (snapshot.task_id !== task.id || snapshot.session !== task.sessionName || !Number.isInteger(snapshot.last_seq)) {
      return { kind: 'unavailable' }
    }
    if (snapshot.status === 'interrupted') return { kind: 'interrupted' }
    if (!RUNNER_STATUSES.has(snapshot.status)) return { kind: 'unavailable' }
    return { kind: 'tracked', snapshot }
  } catch (error) {
    if (error && typeof error === 'object' && 'status' in error && error.status === 404) {
      return { kind: 'missing' }
    }
    return { kind: 'unavailable' }
  }
}

/** 仅恢复自动对账产生的 SESSION_LOST；真实成功、失败和取消不被重新打开。 */
export function sessionLostResumeStatus(
  task: TrackedTask & { status: string, errorCode: string | null, lastSeq: number },
  tracking: TaskTracking,
): 'queued' | 'running' | null {
  if (task.status !== 'orphaned' || task.errorCode !== 'SESSION_LOST' || tracking.kind !== 'tracked') {
    return null
  }
  const snapshot = tracking.snapshot
  if (snapshot.task_id !== task.id || snapshot.session !== task.sessionName || snapshot.last_seq < task.lastSeq) {
    return null
  }
  // 已终态的任务也恢复订阅，通过持久化事件补齐产物、错误和最终状态。
  return snapshot.status === 'queued' ? 'queued' : 'running'
}
