import type { MigrationInterface, QueryRunner } from 'typeorm'

// doc/03 §5：001_create_users_and_tokens
// 手写 SQL 建 users + refresh_tokens，字段/索引/外键严格对齐 doc/03 §2。
export class CreateUsersAndTokens1725000000001 implements MigrationInterface {
  name = 'CreateUsersAndTokens1725000000001'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE \`users\` (
        \`id\` int NOT NULL AUTO_INCREMENT,
        \`username\` varchar(64) NOT NULL,
        \`passwordHash\` varchar(255) NOT NULL,
        \`role\` enum('admin','operator','viewer') NOT NULL,
        \`isActive\` tinyint(1) NOT NULL DEFAULT 1,
        \`mustChangePassword\` tinyint(1) NOT NULL DEFAULT 0,
        \`createdAt\` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        \`lastLoginAt\` datetime(6) NULL,
        PRIMARY KEY (\`id\`),
        UNIQUE KEY \`UQ_users_username\` (\`username\`)
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    `)
    await queryRunner.query(`
      CREATE TABLE \`refresh_tokens\` (
        \`id\` char(36) NOT NULL COMMENT 'uuid，即 JWT 的 jti',
        \`userId\` int NOT NULL,
        \`tokenHash\` char(64) NOT NULL COMMENT 'SHA-256，不存明文',
        \`expiresAt\` datetime(6) NOT NULL,
        \`revokedAt\` datetime(6) NULL,
        \`userAgent\` varchar(255) NOT NULL,
        \`ip\` varchar(64) NOT NULL,
        PRIMARY KEY (\`id\`),
        KEY \`IDX_refresh_tokens_user_revoked\` (\`userId\`, \`revokedAt\`),
        KEY \`IDX_refresh_tokens_expires\` (\`expiresAt\`),
        CONSTRAINT \`FK_refresh_tokens_user\` FOREIGN KEY (\`userId\`)
          REFERENCES \`users\` (\`id\`) ON DELETE CASCADE ON UPDATE CASCADE
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('DROP TABLE IF EXISTS `refresh_tokens`')
    await queryRunner.query('DROP TABLE IF EXISTS `users`')
  }
}
