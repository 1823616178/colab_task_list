// POST /api/tasks/:id/drive-confirm（operator+）：用户在浏览器完成 Google
// Drive 授权后，通知 Runner 向 drivemount 的伪终端发送 Enter。
import { getRouterParam } from 'h3'
import { requireRole } from '../../../utils/auth'
import { findTask } from '../../../services/taskEvents'
import { sendApiError } from '../../../utils/errors'
import { sendRunnerError } from '../../../utils/runner'
import { runnerRequest } from '../../../services/runnerClient'
import { writeAudit } from '../../../utils/audit'

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  const id = getRouterParam(event, 'id')!
  const task = await findTask(id)
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)

  try {
    const result = await runnerRequest<{
      task_id: string
      status: string
      session: string
    }>(`/tasks/${id}/drive-confirm`, { method: 'POST' })
    await writeAudit(event, { action: 'task.drive-confirm', target: id })
    return result
  } catch (error) {
    return sendRunnerError(event, error)
  }
})
