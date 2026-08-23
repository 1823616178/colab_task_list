import { promises as fs } from 'node:fs'
import { In, IsNull, Not } from 'typeorm'
import type { Task } from '../entities/Task'
import { TaskSchema } from '../entities/Task'
import { getDataSource } from '../utils/db'
import { writeAudit } from '../utils/audit'
import { ftpConfigFromRuntime, uploadOutputFile } from './ftpTransfer'
import type { FtpUploadConfig } from './ftpTransfer'
import { resolveTaskFilePath, taskOutputRoot } from './taskFiles'

const MAX_ERROR_LENGTH = 2_000
const RETRY_DELAYS_MS = [0, 2_000, 10_000]

declare global {
  var __colabFtpUploadQueue: Promise<void> | undefined
  var __colabFtpQueuedTasks: Set<string> | undefined
}

function queuedTasks(): Set<string> {
  globalThis.__colabFtpQueuedTasks ??= new Set()
  return globalThis.__colabFtpQueuedTasks
}

function errorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.slice(0, MAX_ERROR_LENGTH)
}

async function wait(delayMs: number): Promise<void> {
  if (delayMs <= 0) return
  await new Promise(resolve => setTimeout(resolve, delayMs))
}

async function performTaskFtpUpload(task: Task, config: FtpUploadConfig): Promise<void> {
  const localPath = resolveTaskFilePath(
    task.outputZipPath,
    taskOutputRoot(),
    task.id,
    'output.zip',
  )
  if (!localPath || !(await fs.stat(localPath).catch(() => null))?.isFile()) {
    await (await getDataSource()).getRepository(TaskSchema).update(
      { id: task.id },
      { ftpStatus: 'failed', ftpError: '本地产物不存在，无法上传 FTP' },
    )
    return
  }

  const repo = (await getDataSource()).getRepository(TaskSchema)
  // 数据库原子抢占：部署交接期间即使短暂存在两个 Nitro 进程，也只有一个上传者。
  const claim = await repo.update(
    { id: task.id, ftpStatus: 'pending' },
    { ftpStatus: 'uploading', ftpError: null },
  )
  if (claim.affected !== 1) return

  let lastError = ''
  for (const delayMs of RETRY_DELAYS_MS) {
    await wait(delayMs)
    await repo.increment({ id: task.id }, 'ftpAttempts', 1)
    try {
      const remotePath = await uploadOutputFile(localPath, task.id, config)
      await repo.update({ id: task.id }, {
        ftpStatus: 'uploaded',
        ftpRemotePath: remotePath,
        ftpUploadedAt: new Date(),
        ftpError: null,
      })
      await writeAudit(null, {
        action: 'output.ftp.upload',
        target: task.id,
        detail: { remotePath, sizeBytes: task.outputSizeBytes },
        user: { userId: null, username: 'system' },
      })
      return
    } catch (error) {
      lastError = errorMessage(error)
    }
  }
  await repo.update({ id: task.id }, { ftpStatus: 'failed', ftpError: lastError })
  console.warn(`[ftp-upload] ${task.id} 上传失败（已重试 ${RETRY_DELAYS_MS.length} 次）: ${lastError}`)
}

/**
 * 把成功任务放入单线程 FTP 队列。只等待“入队”，不等待大文件上传完成。
 * 返回 false 表示任务不符合真实成功产物条件或已上传。
 */
export async function scheduleTaskFtpUpload(taskId: string, force = false): Promise<boolean> {
  const repo = (await getDataSource()).getRepository(TaskSchema)
  const task = await repo.findOne({ where: { id: taskId } })
  if (!task
    || task.status !== 'succeeded'
    || !task.outputZipPath
    || !['native', 'cli'].includes(task.transportMode ?? '')) {
    return false
  }
  if (!force && task.ftpStatus === 'uploaded') return false
  // 另一个服务进程已抢占时不重置为 pending；失败后可由历史页强制重试。
  if (!force && task.ftpStatus === 'uploading') return false
  if (queuedTasks().has(taskId)) return true

  const config = ftpConfigFromRuntime(useRuntimeConfig() as Record<string, unknown>)
  if (!config) {
    await repo.update({ id: taskId }, {
      ftpStatus: 'failed',
      ftpError: 'FTP 未配置，请检查服务端环境变量',
    })
    return false
  }
  await repo.update({ id: taskId }, {
    ftpStatus: 'pending',
    ftpRemotePath: force ? null : task.ftpRemotePath,
    ftpUploadedAt: force ? null : task.ftpUploadedAt,
    ftpError: null,
  })

  queuedTasks().add(taskId)
  const previous = globalThis.__colabFtpUploadQueue ?? Promise.resolve()
  const job = previous
    .catch(() => {})
    .then(() => performTaskFtpUpload(task, config))
    .catch(error => console.error(`[ftp-upload] ${taskId} 队列异常:`, errorMessage(error)))
    .finally(() => queuedTasks().delete(taskId))
  globalThis.__colabFtpUploadQueue = job
  return true
}

/** Nitro 启动时补传停机窗口内和历史迁移标记为 pending 的真实产物。 */
export async function recoverFtpUploads(): Promise<number> {
  const tasks = await (await getDataSource()).getRepository(TaskSchema).find({
    where: [
      {
        status: 'succeeded',
        transportMode: In(['native', 'cli']),
        outputZipPath: Not(IsNull()),
        ftpStatus: IsNull(),
      },
      {
        status: 'succeeded',
        transportMode: In(['native', 'cli']),
        outputZipPath: Not(IsNull()),
        ftpStatus: In(['pending', 'uploading', 'failed']),
      },
    ],
    order: { finishedAt: 'ASC' },
  })
  for (const task of tasks) await scheduleTaskFtpUpload(task.id)
  return tasks.length
}
