import type { H3Event } from 'h3'
import { sendApiError } from './errors'
import type { ApiErrorBody } from './errors'
import type { User, UserRole } from '../entities/User'

declare module 'h3' {
  interface H3EventContext {
    /** 由 server/middleware/auth.ts 在 JWT 校验通过后注入 */
    auth?: { userId: number, username: string, role: UserRole }
  }
}

/** 出参用的用户信息（绝不带 passwordHash） */
export interface PublicUser {
  id: number
  username: string
  role: UserRole
  isActive: boolean
  mustChangePassword: boolean
  createdAt: Date
  lastLoginAt: Date | null
}

export function toPublicUser(user: User): PublicUser {
  return {
    id: user.id,
    username: user.username,
    role: user.role,
    isActive: user.isActive,
    mustChangePassword: user.mustChangePassword,
    createdAt: user.createdAt,
    lastLoginAt: user.lastLoginAt,
  }
}

/**
 * role 守卫（doc/03 §2 角色权限表）。用法：
 *   const denied = requireRole(event, ['admin'])
 *   if (denied) return denied
 * P3 任务路由用 requireRole(event, ['admin', 'operator']) 把 viewer 挡在写操作外。
 */
export function requireRole(event: H3Event, roles: UserRole[]): ApiErrorBody | null {
  const auth = event.context.auth
  if (!auth) return sendApiError(event, 401, 'UNAUTHORIZED', '未登录或登录已过期')
  if (!roles.includes(auth.role)) {
    return sendApiError(event, 403, 'FORBIDDEN', '当前角色无权执行该操作')
  }
  return null
}
