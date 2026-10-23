// POST /api/tasks/:id/redownload（operator+）：转发 Runner（包装 --download-only），
// 任务转回 running 并重建 SSE 订阅（doc/05 P3）。
import { getRouterParam, setResponseStatus } from 'h3'
import { TaskSchema } from '../../../entities/Task'
import { requireRole } from '../../../utils/auth'
import { getDataSource } from '../../../utils/db'
import { findTask } from '../../../services/taskEvents'
import { sendApiError } from '../../../utils/errors'
import { sendRunnerError } from '../../../utils/runner'
import { runnerRequest } from '../../../services/runnerClient'
import { ensureTaskSubscription, stopTaskSubscription } from '../../../services/taskSubscription'
import { writeAudit } from '../../../utils/audit'

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  const id = getRouterParam(event, 'id')!
  const task = await findTask(id)
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)
  if (task.status !== 'recoverable') {
    return sendApiError(event, 409, 'REDOWNLOAD_NOT_ALLOWED',
      `仅 recoverable 状态允许重取结果（当前 ${task.status}）`)
  }

  let result: { task_id: string, status: string }
  try {
    result = await runnerRequest<{ task_id: string, status: string }>(
      `/tasks/${id}/redownload`,
      { method: 'POST' },
    )
  } catch (error) {
    return sendRunnerError(event, error)
  }

  // Runner 侧事件流重新打开（redownloading）：DB 转回 running 并重建订阅，
  // 终态（task.done / task.failed）由订阅链路再次收尾
  const ds = await getDataSource()
  stopTaskSubscription(id)
  await ds.getRepository(TaskSchema).update(
    { id, status: 'recoverable', lastSeq: task.lastSeq },
    { status: 'running', currentPhase: 'output.transfer', finishedAt: null, errorCode: null, errorMessage: null },
  )
  ensureTaskSubscription(id, task.lastSeq)

  await writeAudit(event, { action: 'task.redownload', target: id })

  setResponseStatus(event, 202)
  return result
})
