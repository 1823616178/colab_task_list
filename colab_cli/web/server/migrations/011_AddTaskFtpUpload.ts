import type { MigrationInterface, QueryRunner } from 'typeorm'

// 成功产物的 FTP 镜像状态；上传失败不改变任务 succeeded 终态。
export class AddTaskFtpUpload1725000000011 implements MigrationInterface {
  name = 'AddTaskFtpUpload1725000000011'

  async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE tasks
      ADD ftpStatus enum('pending','uploading','uploaded','failed') NULL AFTER outputSha256,
      ADD ftpRemotePath varchar(512) NULL AFTER ftpStatus,
      ADD ftpUploadedAt datetime(6) NULL AFTER ftpRemotePath,
      ADD ftpError text NULL AFTER ftpUploadedAt,
      ADD ftpAttempts int NOT NULL DEFAULT 0 AFTER ftpError,
      ADD KEY IDX_tasks_ftp_status (ftpStatus, finishedAt)
    `)
    // 演练任务的 ZIP 只是占位文件，不作为真实生成产物补传。
    await queryRunner.query(`
      UPDATE tasks
      SET ftpStatus = 'pending'
      WHERE status = 'succeeded'
        AND outputZipPath IS NOT NULL
        AND transportMode IN ('native', 'cli')
    `)
  }

  async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE tasks
      DROP KEY IDX_tasks_ftp_status,
      DROP COLUMN ftpAttempts,
      DROP COLUMN ftpError,
      DROP COLUMN ftpUploadedAt,
      DROP COLUMN ftpRemotePath,
      DROP COLUMN ftpStatus
    `)
  }
}
