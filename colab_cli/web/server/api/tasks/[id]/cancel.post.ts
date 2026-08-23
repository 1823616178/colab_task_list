// POST /api/tasks/:id/cancel（operator+，doc/02 §2.3.2）：
// interrupt（默认，restart-kernel，VM 保留可重投）/ destroy（stop，产物丢失）。
// 转发 Runner；终态回写交给 SSE 订阅（task.canceled 事件统一收尾）。
import { getRouterParam, setResponseStatus } from 'h3'
import { requireRole } from '../../../utils/auth'
import { findTask } from '../../../services/taskEvents'
import { sendApiError } from '../../../utils/errors'
import { isApiError, readOptionalJsonBody, sendRunnerError } from '../../../utils/runner'
import { runnerRequest } from '../../../services/runnerClient'
import { writeAudit } from '../../../utils/audit'

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  const id = getRouterParam(event, 'id')!
  const task = await findTask(id)
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)

  const body = await readOptionalJsonBody(event)
  if (isApiError(body)) return body
  const mode = body.mode ?? 'interrupt'
  if (mode !== 'interrupt' && mode !== 'destroy') {
    return sendApiError(event, 400, 'BAD_REQUEST', `未知取消模式: ${String(mode)}（可选: interrupt | destroy）`)
  }

  try {
    const result = await runnerRequest<{ task_id: string, status: string, mode: string }>(
      `/tasks/${id}/cancel`,
      { method: 'POST', body: { mode } },
    )
    await writeAudit(event, { action: 'task.cancel', target: id, detail: { mode } })
    if (result.status === 'canceling') setResponseStatus(event, 202) // running 异步收尾
    return result
  } catch (error) {
    return sendRunnerError(event, error)
  }
})
