import { getDataSource } from '../../utils/db'
import { verifyRefreshToken } from '../../utils/jwt'
import { clearRefreshCookie, readRefreshCookie } from '../../utils/token'
import { RefreshTokenSchema } from '../../entities/RefreshToken'

// POST /api/auth/logout → 撤销 refresh（doc/02 §3.1）
export default defineEventHandler(async (event) => {
  const token = readRefreshCookie(event)
  if (token) {
    try {
      const payload = verifyRefreshToken(useRuntimeConfig(event).jwtSecret, token)
      const repo = (await getDataSource()).getRepository(RefreshTokenSchema)
      const row = await repo.findOne({ where: { id: payload.jti } })
      if (row && !row.revokedAt) {
        row.revokedAt = new Date()
        await repo.save(row)
      }
    } catch {
      // token 已失效也照常登出
    }
  }
  clearRefreshCookie(event)
  return { ok: true }
})
