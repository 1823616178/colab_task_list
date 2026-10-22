import { getRequestIP, readBody, setResponseHeader } from 'h3'
import { getDataSource } from '../../utils/db'
import { sendApiError } from '../../utils/errors'
import { signAccessToken } from '../../utils/jwt'
import { verifyPassword } from '../../utils/password'
import { issueRefreshToken } from '../../utils/token'
import { toPublicUser } from '../../utils/auth'
import { writeAudit } from '../../utils/audit'
import { UserSchema } from '../../entities/User'
import { loginRateLimiter } from '../../utils/loginRateLimit'

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

// POST /api/auth/login { username, password } → { accessToken, user }（doc/02 §3.1）
export default defineEventHandler(async (event) => {
  const body = await readBody<{ username?: unknown, password?: unknown }>(event)
  const username = typeof body?.username === 'string' ? body.username.trim() : ''
  const password = typeof body?.password === 'string' ? body.password : ''
  if (!username || !password) {
    return sendApiError(event, 400, 'VALIDATION_ERROR', '用户名与密码不能为空')
  }

  // 防爆破：IP+用户名 滑动窗口，超限临时锁定（锁定期内不校验密码，避免泄露口令正误）
  const ip = getRequestIP(event, { xForwardedFor: true }) ?? 'unknown'
  const limiter = loginRateLimiter()
  const verdict = limiter.check(ip, username)
  if (!verdict.allowed) {
    const retryAfter = Math.ceil(verdict.retryAfterMs / 1000)
    setResponseHeader(event, 'Retry-After', String(retryAfter))
    await writeAudit(event, {
      action: 'auth.login.failure',
      target: username,
      detail: { reason: 'rate_limited', ip, retryAfterSeconds: retryAfter },
      user: { userId: null, username },
    })
    return sendApiError(event, 429, 'TOO_MANY_ATTEMPTS',
      `登录失败次数过多，请 ${Math.ceil(retryAfter / 60)} 分钟后再试`, { retryAfterSeconds: retryAfter })
  }

  const repo = (await getDataSource()).getRepository(UserSchema)
  const user = await repo.findOne({ where: { username } })
  // 用户不存在与密码错误返回同一文案，避免账号枚举
  if (!user || !(await verifyPassword(user.passwordHash, password))) {
    await writeAudit(event, {
      action: 'auth.login.failure',
      target: username,
      detail: { reason: 'bad_credentials' },
      user: { userId: null, username },
    })
    const { delayMs, locked } = limiter.recordFailure(ip, username)
    await sleep(delayMs) // 渐进延迟：连续失败越多，响应越慢
    if (locked) {
      await writeAudit(event, {
        action: 'auth.login.lockout',
        target: username,
        detail: { ip },
        user: { userId: null, username },
      })
    }
    return sendApiError(event, 401, 'UNAUTHORIZED', '用户名或密码错误')
  }
  if (!user.isActive) {
    await writeAudit(event, {
      action: 'auth.login.failure',
      target: username,
      detail: { reason: 'account_disabled' },
      user: { userId: user.id, username },
    })
    return sendApiError(event, 401, 'UNAUTHORIZED', '账号已停用，请联系管理员')
  }

  limiter.recordSuccess(ip, username)
  user.lastLoginAt = new Date()
  await repo.save(user)

  await issueRefreshToken(event, user)
  await writeAudit(event, {
    action: 'auth.login.success',
    target: username,
    user: { userId: user.id, username },
  })
  return {
    accessToken: signAccessToken(useRuntimeConfig(event).jwtSecret, user),
    user: toPublicUser(user),
  }
})
