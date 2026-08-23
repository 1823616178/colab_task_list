import { getDataSource } from '../../utils/db'
import { requireRole, toPublicUser } from '../../utils/auth'
import { UserSchema } from '../../entities/User'

// GET /api/users —— 用户管理限 admin（doc/01 §7 安全基线）
export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin'])
  if (denied) return denied
  const users = await (await getDataSource())
    .getRepository(UserSchema)
    .find({ order: { id: 'ASC' } })
  return { users: users.map(toPublicUser) }
})
