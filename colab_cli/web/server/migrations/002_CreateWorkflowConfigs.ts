import type { MigrationInterface, QueryRunner } from 'typeorm'

// doc/03 §5：002_create_workflow_configs —— workflows/*.json 的库中镜像。
export class CreateWorkflowConfigs1725000000002 implements MigrationInterface {
  name = 'CreateWorkflowConfigs1725000000002'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE \`workflow_configs\` (
        \`id\` int NOT NULL AUTO_INCREMENT,
        \`slug\` varchar(128) NOT NULL COMMENT '文件名去扩展名，如 minimax_h3',
        \`name\` varchar(255) NOT NULL,
        \`configPath\` varchar(512) NOT NULL COMMENT '相对仓库根',
        \`apiJsonPath\` varchar(512) NOT NULL COMMENT '解析 api_json 后的实际路径',
        \`supportedAccelerators\` json NOT NULL,
        \`acceleratorNote\` text NULL,
        \`inputFields\` json NOT NULL,
        \`comfyFlags\` json NOT NULL,
        \`models\` json NOT NULL COMMENT '原样存，含 size_bytes',
        \`totalModelBytes\` bigint NOT NULL COMMENT '派生，UI 预估耗时',
        \`syncedAt\` datetime(6) NOT NULL,
        PRIMARY KEY (\`id\`),
        UNIQUE KEY \`UQ_workflow_configs_slug\` (\`slug\`)
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('DROP TABLE IF EXISTS `workflow_configs`')
  }
}
