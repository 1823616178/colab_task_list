import { getCookie, getHeader, getRequestIP, setCookie, deleteCookie } from 'h3'
import type { H3Event } from 'h3'
import { getDataSource } from './db'
import {
  REFRESH_TOKEN_TTL_SEC,
  sha256Hex,
  signRefreshToken,
} from './jwt'
import { RefreshTokenSchema } from '../entities/RefreshToken'
import type { User } from '../entities/User'

export const REFRESH_COOKIE_NAME = 'colab_refresh'

function isSecureCookie(event: H3Event): boolean {
  const value = useRuntimeConfig(event).authCookieSecure
  return value !== false && String(value) !== 'false'
}

/** 签发 refresh token：JWT（jti=行 id）+ SHA-256 哈希入库 + HttpOnly Cookie */
export async function issueRefreshToken(event: H3Event, user: User): Promise<void> {
  const config = useRuntimeConfig(event)
  const { token, jti } = signRefreshToken(config.jwtSecret, user.id)
  const repo = (await getDataSource()).getRepository(RefreshTokenSchema)
  await repo.save({
    id: jti,
    userId: user.id,
    tokenHash: sha256Hex(token),
    expiresAt: new Date(Date.now() + REFRESH_TOKEN_TTL_SEC * 1000),
    revokedAt: null,
    userAgent: (getHeader(event, 'user-agent') ?? '').slice(0, 255),
    ip: (getRequestIP(event) ?? '').slice(0, 64),
  })
  setCookie(event, REFRESH_COOKIE_NAME, token, {
    httpOnly: true,
    secure: isSecureCookie(event), // 生产 HTTPS 必为 true；本地 http 调试见 README
    sameSite: 'strict',
    path: '/api/auth', // 只在认证端点回传，缩小暴露面
    maxAge: REFRESH_TOKEN_TTL_SEC,
  })
}

export function readRefreshCookie(event: H3Event): string | undefined {
  return getCookie(event, REFRESH_COOKIE_NAME)
}

export function clearRefreshCookie(event: H3Event): void {
  deleteCookie(event, REFRESH_COOKIE_NAME, { path: '/api/auth' })
}

/** 撤销指定用户的全部有效 refresh token（重用检测/改密/停用账号时用） */
export async function revokeAllRefreshTokens(userId: number): Promise<void> {
  const repo = (await getDataSource()).getRepository(RefreshTokenSchema)
  await repo
    .createQueryBuilder()
    .update()
    .set({ revokedAt: new Date() })
    .where('`userId` = :userId AND `revokedAt` IS NULL', { userId })
    .execute()
}

/**
 * 删除已过期的 refresh_tokens 行（走 `(expiresAt)` 索引）。
 * 未过期但已撤销的行必须保留到过期——重用检测依赖它们。
 */
export async function purgeExpiredRefreshTokens(now: Date = new Date()): Promise<number> {
  const repo = (await getDataSource()).getRepository(RefreshTokenSchema)
  const result = await repo
    .createQueryBuilder()
    .delete()
    .where('`expiresAt` < :now', { now })
    .execute()
  return result.affected ?? 0
}
