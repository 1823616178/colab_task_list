import type { MigrationInterface, QueryRunner } from 'typeorm'

// doc/05 P5：008_create_audit_log —— 操作审计（谁在什么时候开了 A100）。
// userId 不设 FK：用户行被清理（停用而非删除是惯例，但允许物理删测试账号）时
// 审计记录必须留存；detail 为 json，记录与动作相关的非敏感上下文（绝不存密码）。
export class CreateAuditLog1725000000008 implements MigrationInterface {
  name = 'CreateAuditLog1725000000008'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE \`audit_log\` (
        \`id\` bigint NOT NULL AUTO_INCREMENT,
        \`userId\` int NULL COMMENT '操作者；系统动作（如产物清理）为 NULL',
        \`username\` varchar(64) NOT NULL COMMENT '冗余快照；系统动作为 system',
        \`action\` varchar(64) NOT NULL COMMENT '如 auth.login.success / task.submit / output.cleanup',
        \`target\` varchar(255) NULL COMMENT '操作对象（task_id / slug / 用户名…）',
        \`detail\` json NULL COMMENT '动作上下文（取消模式、同步数量等）',
        \`ip\` varchar(64) NOT NULL DEFAULT '' COMMENT '系统动作为空串',
        \`createdAt\` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        PRIMARY KEY (\`id\`),
        KEY \`IDX_audit_log_created\` (\`createdAt\`),
        KEY \`IDX_audit_log_user_created\` (\`userId\`, \`createdAt\`)
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('DROP TABLE IF EXISTS `audit_log`')
  }
}
