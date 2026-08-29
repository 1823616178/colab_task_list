import { EntitySchema } from 'typeorm'
import type { User } from './User'

// doc/03 §2 RefreshToken 表：refresh token 哈希入库、可撤销、轮换 + 重用检测。
export interface RefreshToken {
  /** uuid，即 JWT 的 jti */
  id: string
  userId: number
  /** SHA-256(token)，不存明文 */
  tokenHash: string
  expiresAt: Date
  /** 登出/轮换时置位 */
  revokedAt: Date | null
  userAgent: string
  ip: string
  /** many-to-one → users.id（onDelete CASCADE）。
   *  仅为让 EntitySchema<RefreshToken> 的 relations 键落在 keyof 内而声明，
   *  代码里一律按 userId 查，不做 eager/relations 加载，所以是可选的。 */
  user?: User
}

export const RefreshTokenSchema = new EntitySchema<RefreshToken>({
  name: 'RefreshToken',
  tableName: 'refresh_tokens',
  columns: {
    id: { type: 'char', length: 36, primary: true },
    userId: { type: 'int' },
    tokenHash: { type: 'char', length: 64 },
    expiresAt: { type: 'datetime' },
    revokedAt: { type: 'datetime', nullable: true },
    userAgent: { type: 'varchar', length: 255 },
    ip: { type: 'varchar', length: 64 },
  },
  relations: {
    user: {
      type: 'many-to-one',
      target: 'User',
      joinColumn: { name: 'userId' },
      onDelete: 'CASCADE',
    },
  },
  indices: [
    // (userId, revokedAt)：查用户有效 token / 全量撤销
    { name: 'IDX_refresh_tokens_user_revoked', columns: ['userId', 'revokedAt'] },
    // (expiresAt)：过期清理
    { name: 'IDX_refresh_tokens_expires', columns: ['expiresAt'] },
  ],
})
