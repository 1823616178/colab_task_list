import { createHash, randomUUID } from 'node:crypto'
import jwt from 'jsonwebtoken'
import type { UserRole } from '../entities/User'

// doc/02 §3.1：access 15 分钟（只存 Pinia 内存）；refresh 7 天（HttpOnly Cookie）
export const ACCESS_TOKEN_TTL = '15m'
export const REFRESH_TOKEN_TTL_SEC = 7 * 24 * 60 * 60

export interface AccessTokenPayload {
  sub: number
  username: string
  role: UserRole
  type: 'access'
}

export interface RefreshTokenPayload {
  sub: number
  type: 'refresh'
  jti: string
}

const ROLES: readonly string[] = ['admin', 'operator', 'viewer']

export function signAccessToken(
  secret: string,
  user: { id: number, username: string, role: UserRole },
): string {
  const payload: AccessTokenPayload = {
    sub: user.id,
    username: user.username,
    role: user.role,
    type: 'access',
  }
  return jwt.sign(payload, secret, { expiresIn: ACCESS_TOKEN_TTL })
}

export function verifyAccessToken(secret: string, token: string): AccessTokenPayload {
  const payload = jwt.verify(token, secret)
  if (
    typeof payload !== 'object' || payload === null
    || (payload as Record<string, unknown>).type !== 'access'
    || typeof (payload as Record<string, unknown>).sub !== 'number'
    || typeof (payload as Record<string, unknown>).username !== 'string'
    || !ROLES.includes(String((payload as Record<string, unknown>).role))
  ) {
    throw new Error('invalid access token payload')
  }
  return payload as unknown as AccessTokenPayload
}

export function signRefreshToken(secret: string, userId: number): { token: string, jti: string } {
  const jti = randomUUID()
  const token = jwt.sign({ sub: userId, type: 'refresh' }, secret, {
    expiresIn: REFRESH_TOKEN_TTL_SEC,
    jwtid: jti,
  })
  return { token, jti }
}

export function verifyRefreshToken(secret: string, token: string): RefreshTokenPayload {
  const payload = jwt.verify(token, secret)
  if (
    typeof payload !== 'object' || payload === null
    || (payload as Record<string, unknown>).type !== 'refresh'
    || typeof (payload as Record<string, unknown>).sub !== 'number'
    || typeof (payload as Record<string, unknown>).jti !== 'string'
  ) {
    throw new Error('invalid refresh token payload')
  }
  return payload as unknown as RefreshTokenPayload
}

/** refresh token 入库前的 SHA-256（明文绝不出库/日志，doc/03 §2） */
export function sha256Hex(text: string): string {
  return createHash('sha256').update(text).digest('hex')
}
