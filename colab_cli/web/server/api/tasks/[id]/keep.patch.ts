// PATCH /api/tasks/:id/keep { keep: boolean }：任务级「永久保留产物」开关（operator+）。
import { getRouterParam, readBody } from 'h3'
import { TaskSchema } from '../../../entities/Task'
import { findTask } from '../../../services/taskEvents'
import { requireRole } from '../../../utils/auth'
import { getDataSource } from '../../../utils/db'
import { sendApiError } from '../../../utils/errors'
import { writeAudit } from '../../../utils/audit'

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  const id = getRouterParam(event, 'id')!
  const body = await readBody<{ keep?: unknown }>(event)
  if (typeof body?.keep !== 'boolean') {
    return sendApiError(event, 400, 'VALIDATION_ERROR', 'keep 必须是布尔值')
  }
  const task = await findTask(id)
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)
  await (await getDataSource()).getRepository(TaskSchema).update({ id }, { keepOutput: body.keep })
  await writeAudit(event, {
    action: body.keep ? 'output.keep' : 'output.unkeep',
    target: id,
  })
  return { ok: true, keepOutput: body.keep }
})
