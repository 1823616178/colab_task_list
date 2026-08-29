import { getHeader, getRequestURL } from 'h3'
import { sendApiError } from '../utils/errors'
import { verifyAccessToken } from '../utils/jwt'

// 全局 JWT 中间件（doc/02 §3）：除 /api/auth/login、/api/auth/refresh 外，
// 全部 /api/** 必须携带有效 access token。
export default defineEventHandler((event) => {
  const path = getRequestURL(event).pathname
  if (!path.startsWith('/api/')) return
  if (path === '/api/auth/login' || path === '/api/auth/refresh') return

  const header = getHeader(event, 'authorization')
  const token = header?.startsWith('Bearer ') ? header.slice(7).trim() : ''
  if (!token) return sendApiError(event, 401, 'UNAUTHORIZED', '缺少访问令牌')

  try {
    const payload = verifyAccessToken(useRuntimeConfig(event).jwtSecret, token)
    event.context.auth = { userId: payload.sub, username: payload.username, role: payload.role }
  } catch {
    return sendApiError(event, 401, 'UNAUTHORIZED', '访问令牌无效或已过期')
  }
})
