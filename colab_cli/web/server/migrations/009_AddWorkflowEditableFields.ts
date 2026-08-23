import type { MigrationInterface, QueryRunner } from 'typeorm'

// 工作流声明可在任务提交页覆盖的 API 节点输入；文件仍是真相，数据库只做镜像。
export class AddWorkflowEditableFields1725000000009 implements MigrationInterface {
  name = 'AddWorkflowEditableFields1725000000009'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE \`workflow_configs\`
      ADD \`editableFields\` json NULL AFTER \`inputFields\`
    `)
    await queryRunner.query(`
      UPDATE \`workflow_configs\`
      SET \`editableFields\` = JSON_ARRAY()
      WHERE \`editableFields\` IS NULL
    `)
    await queryRunner.query(`
      ALTER TABLE \`workflow_configs\`
      MODIFY \`editableFields\` json NOT NULL
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE \`workflow_configs\`
      DROP COLUMN \`editableFields\`
    `)
  }
}
