// GET /api/tasks/:id（doc/02 §3.2）：DB 行即真相（事件流实时回写）。
import { getRouterParam } from 'h3'
import { findTask } from '../../../services/taskEvents'
import { sendApiError } from '../../../utils/errors'

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')!
  const task = await findTask(id)
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)
  return { task }
})
