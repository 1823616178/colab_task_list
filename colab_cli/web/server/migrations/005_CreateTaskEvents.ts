import type { MigrationInterface, QueryRunner } from 'typeorm'

// doc/03 §5：005_create_task_events —— 只存里程碑；唯一索引 (taskId, seq)
// 兼作 SSE 重放去重。status 枚举只含 start/ok/warn/error，DB 层就装不下
// progress（应用层另有断言，见 services/taskEvents.ts）。
export class CreateTaskEvents1725000000005 implements MigrationInterface {
  name = 'CreateTaskEvents1725000000005'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE \`task_events\` (
        \`id\` bigint NOT NULL AUTO_INCREMENT,
        \`taskId\` char(24) NOT NULL,
        \`seq\` int NOT NULL,
        \`ts\` datetime(3) NOT NULL COMMENT 'UTC，毫秒精度',
        \`phase\` varchar(32) NOT NULL,
        \`status\` enum('start','ok','warn','error') NOT NULL,
        \`message\` text NOT NULL,
        \`data\` json NULL,
        PRIMARY KEY (\`id\`),
        UNIQUE KEY \`UQ_task_events_task_seq\` (\`taskId\`, \`seq\`),
        CONSTRAINT \`FK_task_events_task\` FOREIGN KEY (\`taskId\`)
          REFERENCES \`tasks\` (\`id\`) ON DELETE CASCADE ON UPDATE CASCADE
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('DROP TABLE IF EXISTS `task_events`')
  }
}
