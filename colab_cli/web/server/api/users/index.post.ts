import { readBody, setResponseStatus } from 'h3'
import { getDataSource } from '../../utils/db'
import { sendApiError } from '../../utils/errors'
import { hashPassword } from '../../utils/password'
import { requireRole, toPublicUser } from '../../utils/auth'
import { writeAudit } from '../../utils/audit'
import { MIN_PASSWORD_LENGTH, USERNAME_PATTERN, UserSchema } from '../../entities/User'
import type { UserRole } from '../../entities/User'

const ROLES: readonly UserRole[] = ['admin', 'operator', 'viewer']

// POST /api/users { username, password, role } —— admin 限定
// 新建用户 mustChangePassword=true：首次登录强制改密
export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin'])
  if (denied) return denied

  const body = await readBody<{ username?: unknown, password?: unknown, role?: unknown }>(event)
  const username = typeof body?.username === 'string' ? body.username.trim() : ''
  const password = typeof body?.password === 'string' ? body.password : ''
  const role = typeof body?.role === 'string' ? body.role : ''

  if (!USERNAME_PATTERN.test(username)) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', '用户名须为 1–64 位字母、数字或 _ . -')
  }
  if (password.length < MIN_PASSWORD_LENGTH) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', `密码长度至少 ${MIN_PASSWORD_LENGTH} 位`)
  }
  if (!ROLES.includes(role as UserRole)) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', 'role 必须是 admin / operator / viewer')
  }

  const repo = (await getDataSource()).getRepository(UserSchema)
  if (await repo.findOne({ where: { username } })) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', '用户名已存在')
  }

  const user = await repo.save({
    username,
    passwordHash: await hashPassword(password),
    role: role as UserRole,
    isActive: true,
    mustChangePassword: true,
  })
  await writeAudit(event, { action: 'user.create', target: username, detail: { role } })
  setResponseStatus(event, 201)
  return { user: toPublicUser(user) }
})
