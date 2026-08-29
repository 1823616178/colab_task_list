import type { MigrationInterface, QueryRunner } from 'typeorm'

// doc/03 §5：004_create_tasks。
// ⚠️ activeSessionKey 生成列 + 唯一索引：MySQL 无部分唯一约束，
// 用它实现「同一 session 只允许一个非终态任务」（非终态=queued/running）。
export class CreateTasks1725000000004 implements MigrationInterface {
  name = 'CreateTasks1725000000004'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE \`tasks\` (
        \`id\` char(24) NOT NULL COMMENT 'create_task_id() 格式，非自增',
        \`workflowConfigId\` int NOT NULL,
        \`workflowName\` varchar(255) NOT NULL COMMENT '冗余快照',
        \`sessionName\` varchar(128) NOT NULL COMMENT '弱引用，不设 FK',
        \`acceleratorKind\` enum('gpu','tpu') NOT NULL,
        \`acceleratorSpec\` varchar(16) NOT NULL,
        \`mountDrive\` tinyint(1) NOT NULL DEFAULT 1,
        \`reuse\` tinyint(1) NOT NULL DEFAULT 0,
        \`keep\` tinyint(1) NOT NULL DEFAULT 0,
        \`timeout\` double NOT NULL DEFAULT 86400,
        \`status\` enum('queued','running','succeeded','failed','recoverable','canceled','orphaned') NOT NULL DEFAULT 'queued',
        \`currentPhase\` varchar(32) NULL COMMENT '最后一个 start 事件的 phase',
        \`queuedAt\` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        \`startedAt\` datetime(6) NULL,
        \`finishedAt\` datetime(6) NULL,
        \`durationMs\` bigint NULL COMMENT '派生',
        \`exitCode\` int NULL,
        \`apiJsonSnapshotPath\` varchar(512) NOT NULL COMMENT '提交瞬间的 api.json 快照',
        \`workflowSnapshotPath\` varchar(512) NOT NULL,
        \`eventsPath\` varchar(512) NOT NULL COMMENT 'colab_output/<id>/events.jsonl',
        \`lastSeq\` int NOT NULL DEFAULT -1 COMMENT '已入库的最大 seq，重连对账用',
        \`cancelMode\` enum('interrupt','destroy') NULL,
        \`outputZipPath\` varchar(512) NULL,
        \`outputSizeBytes\` bigint NULL,
        \`outputSha256\` char(64) NULL,
        \`logPath\` varchar(512) NULL COMMENT 'colab_output/<id>/run.log',
        \`sessionKept\` tinyint(1) NOT NULL DEFAULT 0 COMMENT '失败保护是否触发',
        \`transportMode\` enum('native','cli','dry_run') NULL COMMENT '实际使用的通道',
        \`errorCode\` varchar(255) NULL,
        \`errorMessage\` text NULL,
        \`createdByUserId\` int NULL,
        \`activeSessionKey\` varchar(160) GENERATED ALWAYS AS (
          IF(\`status\` IN ('queued','running'), \`sessionName\`, NULL)
        ) STORED COMMENT '生成列：非终态时=sessionName，配合唯一索引实现 session 互斥',
        PRIMARY KEY (\`id\`),
        UNIQUE KEY \`UQ_tasks_active_session\` (\`activeSessionKey\`),
        KEY \`IDX_tasks_status_queued\` (\`status\`, \`queuedAt\`),
        KEY \`IDX_tasks_session_status\` (\`sessionName\`, \`status\`),
        KEY \`IDX_tasks_workflow_config\` (\`workflowConfigId\`),
        KEY \`IDX_tasks_created_by\` (\`createdByUserId\`),
        CONSTRAINT \`FK_tasks_workflow_config\` FOREIGN KEY (\`workflowConfigId\`)
          REFERENCES \`workflow_configs\` (\`id\`) ON DELETE RESTRICT ON UPDATE CASCADE,
        CONSTRAINT \`FK_tasks_created_by\` FOREIGN KEY (\`createdByUserId\`)
          REFERENCES \`users\` (\`id\`) ON DELETE SET NULL ON UPDATE CASCADE
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('DROP TABLE IF EXISTS `tasks`')
  }
}
