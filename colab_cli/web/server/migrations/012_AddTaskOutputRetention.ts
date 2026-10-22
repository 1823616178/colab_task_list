import type { MigrationInterface, QueryRunner } from 'typeorm'

// 产物保留策略：
// - keepOutput：任务级「永久保留产物」标记，定时清理跳过；
// - outputDownloadedAt：首次从控制台下载产物 ZIP 的时间。
//   既未下载、FTP 也未镜像成功的成功产物按延长保留期处理（outputCleanup）。
// - activeSessionKey 改为普通索引：Runner 支持同会话 FIFO 排队后，同一会话
//   可以同时存在多个 queued 任务，「同会话只一个在跑」由 Runner 保证。
export class AddTaskOutputRetention1725000000012 implements MigrationInterface {
  name = 'AddTaskOutputRetention1725000000012'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE tasks
      ADD keepOutput tinyint NOT NULL DEFAULT 0 AFTER ftpAttempts,
      ADD outputDownloadedAt datetime(6) NULL AFTER keepOutput
    `)
    await queryRunner.query(`
      ALTER TABLE tasks
      DROP INDEX UQ_tasks_active_session,
      ADD KEY IDX_tasks_active_session (activeSessionKey)
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    // 回滚前须保证同会话最多一个 queued/running，否则唯一索引重建失败
    await queryRunner.query(`
      ALTER TABLE tasks
      DROP INDEX IDX_tasks_active_session,
      ADD UNIQUE KEY UQ_tasks_active_session (activeSessionKey)
    `)
    await queryRunner.query(`
      ALTER TABLE tasks
      DROP COLUMN outputDownloadedAt,
      DROP COLUMN keepOutput
    `)
  }
}
