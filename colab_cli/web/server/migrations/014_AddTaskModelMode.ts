import type { MigrationInterface, QueryRunner } from 'typeorm'

// 任务级模型获取方式：auto / download / symlink / copy。
// 历史任务保持 auto，由执行后端沿用原有策略。
export class AddTaskModelMode1725000000014 implements MigrationInterface {
  name = 'AddTaskModelMode1725000000014'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE tasks
      ADD modelMode varchar(16) NOT NULL DEFAULT 'auto' AFTER mountDrive
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('ALTER TABLE tasks DROP COLUMN modelMode')
  }
}
