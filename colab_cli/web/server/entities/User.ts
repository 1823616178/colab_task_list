import { EntitySchema } from 'typeorm'

// doc/03 §2 User 表。实体即 schema 真相（P2 用 EntitySchema 而非装饰器，
// 规避 nitro/esbuild 对 emitDecoratorMetadata 支持不全的构建风险）。
export type UserRole = 'admin' | 'operator' | 'viewer'

export interface User {
  id: number
  username: string
  passwordHash: string
  role: UserRole
  isActive: boolean
  /** 首次登录强制改密（007 seed 与 admin 新建用户置 true） */
  mustChangePassword: boolean
  createdAt: Date
  lastLoginAt: Date | null
}

export const USERNAME_PATTERN = /^[a-zA-Z0-9_.-]{1,64}$/

/** 密码最小长度（登录无此限制，仅设置/修改密码时校验） */
export const MIN_PASSWORD_LENGTH = 8

export const UserSchema = new EntitySchema<User>({
  name: 'User',
  tableName: 'users',
  columns: {
    id: { type: 'int', primary: true, generated: 'increment' },
    username: { type: 'varchar', length: 64, unique: true },
    passwordHash: { type: 'varchar', length: 255 },
    role: { type: 'enum', enum: ['admin', 'operator', 'viewer'] },
    isActive: { type: 'boolean', default: true },
    mustChangePassword: { type: 'boolean', default: false },
    createdAt: { type: 'datetime', createDate: true },
    lastLoginAt: { type: 'datetime', nullable: true },
  },
})
