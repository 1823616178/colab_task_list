import type { MigrationInterface, QueryRunner } from 'typeorm'

// 用户级工作流参数草稿：同一用户、同一工作流只保留一份最新内容。
export class CreateWorkflowDrafts1725000000010 implements MigrationInterface {
  name = 'CreateWorkflowDrafts1725000000010'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query([
      'CREATE TABLE `workflow_drafts` (',
      '  `id` bigint NOT NULL AUTO_INCREMENT,',
      '  `userId` int NOT NULL,',
      '  `workflowConfigId` int NOT NULL,',
      '  `values` json NOT NULL,',
      '  `createdAt` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),',
      '  `updatedAt` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)',
      '    ON UPDATE CURRENT_TIMESTAMP(6),',
      '  PRIMARY KEY (`id`),',
      '  UNIQUE KEY `UQ_workflow_drafts_user_workflow` (`userId`, `workflowConfigId`),',
      '  KEY `IDX_workflow_drafts_workflow` (`workflowConfigId`),',
      '  CONSTRAINT `FK_workflow_drafts_user` FOREIGN KEY (`userId`)',
      '    REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,',
      '  CONSTRAINT `FK_workflow_drafts_workflow` FOREIGN KEY (`workflowConfigId`)',
      '    REFERENCES `workflow_configs` (`id`) ON DELETE CASCADE ON UPDATE CASCADE',
      ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci',
    ].join('\n'))
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('DROP TABLE IF EXISTS `workflow_drafts`')
  }
}
