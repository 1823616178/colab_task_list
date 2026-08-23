import { readBody } from 'h3'
import { getDataSource } from '../../utils/db'
import { sendApiError } from '../../utils/errors'
import { hashPassword, verifyPassword } from '../../utils/password'
import { clearRefreshCookie, revokeAllRefreshTokens } from '../../utils/token'
import { toPublicUser } from '../../utils/auth'
import { writeAudit } from '../../utils/audit'
import { MIN_PASSWORD_LENGTH, UserSchema } from '../../entities/User'

// POST /api/auth/change-password { oldPassword, newPassword }
// 007 seed 的 admin 与 admin 新建用户均为 mustChangePassword=true，首次登录强制走这里。
// 改密成功即撤销该用户全部 refresh token（含当前会话），前端需重新登录。
export default defineEventHandler(async (event) => {
  const auth = event.context.auth
  if (!auth) return sendApiError(event, 401, 'UNAUTHORIZED', '未登录或登录已过期')

  const body = await readBody<{ oldPassword?: unknown, newPassword?: unknown }>(event)
  const oldPassword = typeof body?.oldPassword === 'string' ? body.oldPassword : ''
  const newPassword = typeof body?.newPassword === 'string' ? body.newPassword : ''
  if (!oldPassword || !newPassword) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', '旧密码与新密码不能为空')
  }
  if (newPassword.length < MIN_PASSWORD_LENGTH) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', `新密码长度至少 ${MIN_PASSWORD_LENGTH} 位`)
  }
  if (newPassword === oldPassword) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', '新密码不能与旧密码相同')
  }

  const repo = (await getDataSource()).getRepository(UserSchema)
  const user = await repo.findOne({ where: { id: auth.userId } })
  if (!user) return sendApiError(event, 401, 'UNAUTHORIZED', '账号不存在')
  if (!(await verifyPassword(user.passwordHash, oldPassword))) {
    return sendApiError(event, 401, 'UNAUTHORIZED', '旧密码错误')
  }

  user.passwordHash = await hashPassword(newPassword)
  user.mustChangePassword = false
  await repo.save(user)
  await revokeAllRefreshTokens(user.id)
  clearRefreshCookie(event)
  await writeAudit(event, { action: 'auth.password.change', target: user.username })
  return { ok: true, user: toPublicUser(user) }
})
