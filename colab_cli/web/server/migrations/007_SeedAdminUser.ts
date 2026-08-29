import type { MigrationInterface, QueryRunner } from 'typeorm'
import { hashPassword } from '../utils/password'

// doc/03 §5：007_seed_admin_user —— 从 env 读初始账号，首次登录强制改密。
// ADMIN_USERNAME / ADMIN_PASSWORD 由 server/plugins/00.env.ts 从 runtimeConfig 兜底注入。
export class SeedAdminUser1725000000007 implements MigrationInterface {
  name = 'SeedAdminUser1725000000007'

  async up(queryRunner: QueryRunner): Promise<void> {
    const username = process.env.ADMIN_USERNAME
    const password = process.env.ADMIN_PASSWORD
    if (!username || !password) {
      console.warn('[migration 007] 未设置 ADMIN_USERNAME/ADMIN_PASSWORD，跳过初始 admin seed')
      return
    }
    const existing: { id: number }[] = await queryRunner.query(
      'SELECT `id` FROM `users` WHERE `username` = ? LIMIT 1',
      [username],
    )
    if (existing.length > 0) return // 幂等：已存在同名账号则跳过
    const passwordHash = await hashPassword(password)
    await queryRunner.query(
      'INSERT INTO `users` (`username`, `passwordHash`, `role`, `isActive`, `mustChangePassword`, `createdAt`)'
        + ' VALUES (?, ?, \'admin\', 1, 1, NOW(6))',
      [username, passwordHash],
    )
    console.log(`[migration 007] 已创建初始管理员 ${username}（mustChangePassword=true，首次登录须改密）`)
  }

  async down(): Promise<void> {
    // 故意不删：migration 回滚不应销毁登录账号
  }
}
