// 任务产物清理策略（doc/05 P5）：colab_output/ 会无限增长。
// 规则：只删「终态」（succeeded/failed/recoverable/canceled/orphaned）且
// finishedAt 早于保留期的任务产物目录 <runnerOutputDir>/<task_id>；
// queued/running 绝不碰（状态过滤 + 生成列外的第二道防线是 finishedAt 非空）。
// 每次实际删除写审计日志（系统用户 username='system'）。
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { In, LessThan } from 'typeorm'
import { TaskSchema, TERMINAL_TASK_STATUSES } from '../entities/Task'
import type { TaskStatus } from '../entities/Task'
import { getDataSource } from '../utils/db'
import { writeAudit } from '../utils/audit'
import { repoRoot } from './workflows'
import { retentionFor } from './outputRetention'
import type { RetentionOptions } from './outputRetention'

export function runnerOutputDir(): string {
  const config = useRuntimeConfig()
  return path.resolve(String(config.runnerOutputDir || '') || path.join(repoRoot(), 'colab_output'))
}

// task_id 格式（doc/02 §1.4）：YYYYMMDD-HHMMSS-<uuid4 前 8 位>
const TASK_ID_PATTERN = /^\d{8}-\d{6}-[0-9a-f]{8}$/

export interface CleanupResult {
  cutoff: Date
  retentionDays: number
  /** 命中的终态超期任务数 */
  candidates: number
  /** 因 keepOutput / 未同步延长保留而跳过的任务 */
  skipped: { taskId: string, reason: 'keep' | 'unsynced' }[]
  /** 实际删除的 task_id 列表 */
  deleted: string[]
  /** 释放的字节数（尽力统计） */
  freedBytes: number
  errors: string[]
}

/** 递归统计目录体积（仅文件；删除前尽力而为，失败按 0 计） */
async function dirSizeBytes(dir: string): Promise<number> {
  let total = 0
  const entries = await fs.readdir(dir, { withFileTypes: true })
  for (const entry of entries) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      total += await dirSizeBytes(full)
    } else if (entry.isFile()) {
      total += (await fs.stat(full)).size
    }
  }
  return total
}

export async function cleanupTerminalTaskOutputs(
  options: RetentionOptions | number,
): Promise<CleanupResult> {
  const retention: RetentionOptions = typeof options === 'number'
    ? { retentionDays: options, unsyncedRetentionDays: options }
    : options
  const retentionDays = retention.retentionDays
  const now = new Date()
  // DB 粗筛用标准保留期；延长保留 / keepOutput 在下面逐条判定
  const cutoff = new Date(now.getTime() - retentionDays * 24 * 60 * 60 * 1000)
  const ds = await getDataSource()
  const tasks = await ds.getRepository(TaskSchema).find({
    where: {
      status: In(TERMINAL_TASK_STATUSES),
      finishedAt: LessThan(cutoff),
    },
    select: {
      id: true,
      status: true,
      finishedAt: true,
      keepOutput: true,
      ftpStatus: true,
      outputZipPath: true,
      outputDownloadedAt: true,
      transportMode: true,
    },
  })

  const root = runnerOutputDir()
  const result: CleanupResult = {
    cutoff,
    retentionDays,
    candidates: tasks.length,
    skipped: [],
    deleted: [],
    freedBytes: 0,
    errors: [],
  }
  for (const task of tasks) {
    const info = retentionFor(task, retention)
    if (info.reason === 'keep' || info.reason === 'unsynced') {
      if (!info.expiresAt || info.expiresAt > now) {
        result.skipped.push({ taskId: task.id, reason: info.reason })
        continue
      }
    }
    // 双保险：task_id 必须是约定格式，且解析后的父目录必须就是输出根
    // （防 DB 行被污染时删到输出根之外的目录）
    const dir = path.resolve(root, task.id)
    if (!TASK_ID_PATTERN.test(task.id) || path.dirname(dir) !== root) {
      result.errors.push(`${task.id}: 路径校验未通过，已跳过`)
      continue
    }
    // 目录不存在：无需删除，也不算错误
    const stat = await fs.stat(dir).catch(() => null)
    if (!stat?.isDirectory()) continue

    const sizeBytes = await dirSizeBytes(dir).catch(() => 0)
    try {
      await fs.rm(dir, { recursive: true, force: true })
      result.deleted.push(task.id)
      result.freedBytes += sizeBytes
      await writeAudit(null, {
        action: 'output.cleanup',
        target: task.id,
        detail: {
          path: dir,
          sizeBytes,
          status: task.status as TaskStatus,
          finishedAt: task.finishedAt,
          retentionDays: info.retentionDays,
          reason: info.reason,
        },
        user: { userId: null, username: 'system' },
      })
    } catch (error) {
      result.errors.push(`${task.id}: ${error instanceof Error ? error.message : String(error)}`)
    }
  }
  return result
}
