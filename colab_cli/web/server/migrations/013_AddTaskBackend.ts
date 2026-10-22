import type { MigrationInterface, QueryRunner } from 'typeorm'

// 多计算后端：tasks.backend 记录任务跑在 colab / kaggle / modal 哪个后端。
// 历史任务全部来自 Colab，默认值 'colab'。
export class AddTaskBackend1725000000013 implements MigrationInterface {
  name = 'AddTaskBackend1725000000013'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE tasks
      ADD backend varchar(16) NOT NULL DEFAULT 'colab' AFTER sessionName
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('ALTER TABLE tasks DROP COLUMN backend')
  }
}
