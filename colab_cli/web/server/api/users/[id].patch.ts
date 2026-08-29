import { getRouterParam, readBody } from 'h3'
import { getDataSource } from '../../utils/db'
import { sendApiError } from '../../utils/errors'
import { hashPassword } from '../../utils/password'
import { revokeAllRefreshTokens } from '../../utils/token'
import { requireRole, toPublicUser } from '../../utils/auth'
import { writeAudit } from '../../utils/audit'
import { MIN_PASSWORD_LENGTH, UserSchema } from '../../entities/User'
import type { UserRole } from '../../entities/User'

const ROLES: readonly UserRole[] = ['admin', 'operator', 'viewer']

// PATCH /api/users/:id —— admin 限定。停用用 isActive，不物理删除（doc/03 §2）。
export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin'])
  if (denied) return denied

  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isInteger(id) || id <= 0) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', '无效的用户 id')
  }

  const body = await readBody<{ isActive?: unknown, role?: unknown, password?: unknown }>(event)
  const hasIsActive = typeof body?.isActive === 'boolean'
  const hasRole = typeof body?.role === 'string'
  const hasPassword = typeof body?.password === 'string' && body.password.length > 0
  if (!hasIsActive && !hasRole && !hasPassword) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', '没有可更新的字段（支持 isActive / role / password）')
  }

  const repo = (await getDataSource()).getRepository(UserSchema)
  const user = await repo.findOne({ where: { id } })
  if (!user) return sendApiError(event, 404, 'NOT_FOUND', '用户不存在')

  // 防自锁：不能停用或降权当前登录的管理员账号
  const auth = event.context.auth
  if (auth && auth.userId === user.id) {
    if (hasIsActive && body!.isActive === false) {
      return sendApiError(event, 400, 'VALIDATION_ERROR', '不能停用当前登录的管理员账号')
    }
    if (hasRole && body!.role !== 'admin') {
      return sendApiError(event, 400, 'VALIDATION_ERROR', '不能降权当前登录的管理员账号')
    }
  }

  if (hasRole) {
    if (!ROLES.includes(body!.role as UserRole)) {
      return sendApiError(event, 400, 'VALIDATION_ERROR', 'role 必须是 admin / operator / viewer')
    }
    user.role = body!.role as UserRole
  }
  if (hasIsActive) user.isActive = body!.isActive as boolean
  if (hasPassword) {
    const password = body!.password as string
    if (password.length < MIN_PASSWORD_LENGTH) {
      return sendApiError(event, 400, 'VALIDATION_ERROR', `密码长度至少 ${MIN_PASSWORD_LENGTH} 位`)
    }
    user.passwordHash = await hashPassword(password)
    user.mustChangePassword = true // 重置密码后首次登录强制改密
  }

  await repo.save(user)
  // 停用/改密/降权都会使旧登录状态失效：撤销该用户全部 refresh token
  if ((hasIsActive && body!.isActive === false) || hasPassword || hasRole) {
    await revokeAllRefreshTokens(user.id)
  }
  await writeAudit(event, {
    action: 'user.update',
    target: user.username,
    detail: {
      ...(hasRole ? { role: user.role } : {}),
      ...(hasIsActive ? { isActive: user.isActive } : {}),
      ...(hasPassword ? { passwordReset: true } : {}),
    },
  })
  return { user: toPublicUser(user) }
})
