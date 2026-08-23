import { DataSource } from 'typeorm'
// 显式静态引入：确保 nitro 生产构建的依赖追踪（nft）能捕获 mysql2 驱动，
// 否则 TypeORM 动态 require 驱动会报 DriverPackageNotInstalledError。
import 'mysql2'
import { UserSchema } from '../entities/User'
import { RefreshTokenSchema } from '../entities/RefreshToken'
import { WorkflowConfigSchema } from '../entities/WorkflowConfig'
import { SessionSchema } from '../entities/Session'
import { TaskSchema } from '../entities/Task'
import { TaskEventSchema } from '../entities/TaskEvent'
import { ModelCacheSchema } from '../entities/ModelCache'
import { AuditLogSchema } from '../entities/AuditLog'
import { WorkflowDraftSchema } from '../entities/WorkflowDraft'
import { CreateUsersAndTokens1725000000001 } from '../migrations/001_CreateUsersAndTokens'
import { CreateWorkflowConfigs1725000000002 } from '../migrations/002_CreateWorkflowConfigs'
import { CreateSessions1725000000003 } from '../migrations/003_CreateSessions'
import { CreateTasks1725000000004 } from '../migrations/004_CreateTasks'
import { CreateTaskEvents1725000000005 } from '../migrations/005_CreateTaskEvents'
import { CreateModelCache1725000000006 } from '../migrations/006_CreateModelCache'
import { SeedAdminUser1725000000007 } from '../migrations/007_SeedAdminUser'
import { CreateAuditLog1725000000008 } from '../migrations/008_CreateAuditLog'
import { AddWorkflowEditableFields1725000000009 } from '../migrations/009_AddWorkflowEditableFields'
import { CreateWorkflowDrafts1725000000010 } from '../migrations/010_CreateWorkflowDrafts'
import { AddTaskFtpUpload1725000000011 } from '../migrations/011_AddTaskFtpUpload'

// globalThis 缓存：dev 热更新时不重复建连
declare global {
  var __colabDataSource: DataSource | undefined
}

let initPromise: Promise<DataSource> | null = null

export function getDataSource(): Promise<DataSource> {
  if (globalThis.__colabDataSource?.isInitialized) {
    return Promise.resolve(globalThis.__colabDataSource)
  }
  if (!initPromise) {
    initPromise = (async () => {
      const config = useRuntimeConfig()
      const dataSource = new DataSource({
        type: 'mysql',
        host: config.dbHost,
        port: Number(config.dbPort),
        username: String(config.dbUser),
        // NUXT_ 环境变量会被 nuxt 自动转型（纯数字密码会变成 number），mysql2 只接受字符串
        password: String(config.dbPassword),
        database: String(config.dbName),
        // doc/03 §1 配置约定：生产禁用 synchronize，一律走 migration
        synchronize: false,
        migrationsRun: true,
        charset: 'utf8mb4', // 工作流名/文件名含中文
        timezone: 'Z', // 全库 UTC，与 task_id 的 UTC 时间戳一致
        logging: false,
        entities: [
          UserSchema,
          RefreshTokenSchema,
          WorkflowConfigSchema,
          SessionSchema,
          TaskSchema,
          TaskEventSchema,
          ModelCacheSchema,
          AuditLogSchema,
          WorkflowDraftSchema,
        ],
        migrations: [
          CreateUsersAndTokens1725000000001,
          CreateWorkflowConfigs1725000000002,
          CreateSessions1725000000003,
          CreateTasks1725000000004,
          CreateTaskEvents1725000000005,
          CreateModelCache1725000000006,
          SeedAdminUser1725000000007,
          CreateAuditLog1725000000008,
          AddWorkflowEditableFields1725000000009,
          CreateWorkflowDrafts1725000000010,
          AddTaskFtpUpload1725000000011,
        ],
      })
      await dataSource.initialize()
      globalThis.__colabDataSource = dataSource
      return dataSource
    })()
  }
  return initPromise
}
