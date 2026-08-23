// GET /api/admin/audit?page=&size=（admin，doc/05 P5）：操作审计只读查询。
// 按 id 倒序（≈ createdAt 倒序）；size 上限 100（与 /api/tasks 同口径）。
import { getQuery } from 'h3'
import { AuditLogSchema } from '../../entities/AuditLog'
import { requireRole } from '../../utils/auth'
import { getDataSource } from '../../utils/db'

const MAX_PAGE_SIZE = 100

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin'])
  if (denied) return denied

  const query = getQuery(event)
  const page = Math.max(1, Number(query.page) || 1)
  const size = Math.min(MAX_PAGE_SIZE, Math.max(1, Number(query.size) || 50))

  const [items, total] = await (await getDataSource())
    .getRepository(AuditLogSchema)
    .findAndCount({ order: { id: 'DESC' }, skip: (page - 1) * size, take: size })
  return { items, total, page, size }
})
