// GET /api/tasks?status=&page=&size=（doc/02 §3.2）：分页 + 状态筛选，queuedAt 倒序。
import { getQuery } from 'h3'
import type { TaskStatus } from '../../entities/Task'
import { TERMINAL_TASK_STATUSES, TaskSchema } from '../../entities/Task'
import { getDataSource } from '../../utils/db'
import { sendApiError } from '../../utils/errors'

const ALL_STATUSES: TaskStatus[] = ['queued', 'running', ...TERMINAL_TASK_STATUSES]

export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const page = Math.max(1, Number(query.page) || 1)
  const size = Math.min(100, Math.max(1, Number(query.size) || 20))
  let status: TaskStatus | undefined
  if (query.status !== undefined && query.status !== '') {
    const value = String(query.status)
    if (!ALL_STATUSES.includes(value as TaskStatus)) {
      return sendApiError(event, 400, 'BAD_REQUEST', `未知任务状态: ${value}（可选: ${ALL_STATUSES.join(', ')}）`)
    }
    status = value as TaskStatus
  }
  const ds = await getDataSource()
  const [items, total] = await ds.getRepository(TaskSchema).findAndCount({
    where: status ? { status } : {},
    order: { queuedAt: 'DESC' },
    skip: (page - 1) * size,
    take: size,
  })
  return { items, total, page, size }
})
