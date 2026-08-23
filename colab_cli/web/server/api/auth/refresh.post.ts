import { getDataSource } from '../../utils/db'
import { sendApiError } from '../../utils/errors'
import { sha256Hex, signAccessToken, verifyRefreshToken } from '../../utils/jwt'
import {
  clearRefreshCookie,
  issueRefreshToken,
  readRefreshCookie,
  revokeAllRefreshTokens,
} from '../../utils/token'
import { RefreshTokenSchema } from '../../entities/RefreshToken'
import { UserSchema } from '../../entities/User'

// POST /api/auth/refresh → { accessToken }（doc/02 §3.1）
// 轮换 + 重用检测（doc/03 §2）：旧 token 立即 revokedAt 置位并签发新 token；
// 收到已撤销的 token → 判定泄露 → 撤销该用户全部 refresh token。
export default defineEventHandler(async (event) => {
  const config = useRuntimeConfig(event)
  const token = readRefreshCookie(event)
  if (!token) return sendApiError(event, 401, 'UNAUTHORIZED', '缺少刷新令牌')

  let payload
  try {
    payload = verifyRefreshToken(config.jwtSecret, token)
  } catch {
    clearRefreshCookie(event)
    return sendApiError(event, 401, 'UNAUTHORIZED', '刷新令牌无效或已过期')
  }

  const ds = await getDataSource()
  const repo = ds.getRepository(RefreshTokenSchema)
  const row = await repo.findOne({ where: { id: payload.jti } })
  if (!row || row.userId !== payload.sub || row.tokenHash !== sha256Hex(token)) {
    clearRefreshCookie(event)
    return sendApiError(event, 401, 'UNAUTHORIZED', '刷新令牌无效')
  }
  if (row.revokedAt) {
    // 重用检测：旧 token 被第二次使用 → 判定泄露，全量撤销
    await revokeAllRefreshTokens(row.userId)
    clearRefreshCookie(event)
    return sendApiError(event, 401, 'UNAUTHORIZED', '检测到刷新令牌重用，已撤销该账号全部登录状态，请重新登录')
  }
  if (row.expiresAt.getTime() <= Date.now()) {
    clearRefreshCookie(event)
    return sendApiError(event, 401, 'UNAUTHORIZED', '刷新令牌已过期，请重新登录')
  }

  const user = await ds.getRepository(UserSchema).findOne({ where: { id: row.userId } })
  if (!user || !user.isActive) {
    clearRefreshCookie(event)
    return sendApiError(event, 401, 'UNAUTHORIZED', '账号不存在或已停用')
  }

  // 轮换：旧 token 置 revokedAt，签发新 refresh + access
  row.revokedAt = new Date()
  await repo.save(row)
  await issueRefreshToken(event, user)
  return { accessToken: signAccessToken(config.jwtSecret, user) }
})
