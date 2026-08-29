import type { MigrationInterface, QueryRunner } from 'typeorm'

// doc/03 §5：006_create_model_cache —— Drive 模型缓存可见性。
export class CreateModelCache1725000000006 implements MigrationInterface {
  name = 'CreateModelCache1725000000006'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE \`model_cache\` (
        \`id\` int NOT NULL AUTO_INCREMENT,
        \`repo\` varchar(255) NOT NULL,
        \`filename\` varchar(512) NOT NULL COMMENT '仓库内路径',
        \`comfyDir\` varchar(64) NOT NULL,
        \`cachePath\` varchar(512) NOT NULL COMMENT 'Drive 内相对路径',
        \`sizeBytes\` bigint NOT NULL,
        \`present\` tinyint(1) NOT NULL DEFAULT 0 COMMENT '上次任务中是否命中',
        \`lastSource\` enum('drive_cache','huggingface','vm_local') NOT NULL,
        \`lastVerifiedAt\` datetime(6) NOT NULL,
        PRIMARY KEY (\`id\`),
        UNIQUE KEY \`UQ_model_cache_repo_filename\` (\`repo\`, \`filename\`)
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query('DROP TABLE IF EXISTS `model_cache`')
  }
}
