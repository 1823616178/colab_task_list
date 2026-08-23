import type { MigrationInterface, QueryRunner } from 'typeorm'

// doc/03 §5：003_create_sessions —— 防孤儿 VM 的核心表（P4 对账落地）。
export class CreateSessions1725000000003 implements MigrationInterface {
  name = 'CreateSessions1725000000003'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE \`sessions\` (
        \`id\` int NOT NULL AUTO_INCREMENT,
        \`name\` varchar(128) NOT NULL,
        \`accelerator\` enum('gpu','tpu','cpu') NULL COMMENT '由 Variant: 解析',
        \`variant\` varchar(32) NULL COMMENT 'A100 / L4 / TPU v6e-1',
        \`endpoint\` varchar(255) NULL COMMENT '名称映射丢失时按 endpoint 停止',
        \`status\` enum('idle','busy','unreachable','stopped') NOT NULL,
        \`expectedAlive\` tinyint(1) NOT NULL DEFAULT 0 COMMENT '我们认为它该活着',
        \`createdAt\` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        \`lastSeenAt\` datetime(6) NULL,
        \`stoppedAt\` datetime(6) NULL,
        \`reconciledAt\` datetime(6) NULL COMMENT '上次对账时间',
        PRIMARY KEY (\`id\`),
        UNIQUE KEY \`UQ_sessions_name\` (\`name\`)
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('DROP TABLE IF EXISTS `sessions`')
  }
}
