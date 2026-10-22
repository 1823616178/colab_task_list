// P5 任务产物清理（doc/05）：每日删除「终态且超过保留期」的任务产物目录
// （<runnerOutputDir>/<task_id>），保留期 NUXT_OUTPUT_RETENTION_DAYS（默认 14 天）。
// 只删终态、绝不碰 queued/running；每次实际删除写审计日志（username='system'）。
// 启动 20 秒后立即跑一轮（覆盖停机窗口），之后每 24 小时一轮；失败只告警。
import { cleanupTerminalTaskOutputs } from '../services/outputCleanup'
import { retentionOptionsFromRuntime } from '../services/outputRetention'

const BOOT_DELAY_MS = 20_000
const DAILY_MS = 24 * 60 * 60 * 1000

export default defineNitroPlugin(() => {
  const config = useRuntimeConfig()
  const retentionDays = Number(config.outputRetentionDays)
  if (!Number.isFinite(retentionDays) || retentionDays <= 0) {
    console.warn(`[output-cleanup] 保留期非法（${String(config.outputRetentionDays)}），定时清理已禁用`)
    return
  }

  const run = async (trigger: string) => {
    try {
      const result = await cleanupTerminalTaskOutputs(retentionOptionsFromRuntime(config))
      if (result.deleted.length > 0 || result.errors.length > 0) {
        console.log(
          `[output-cleanup] ${trigger}：候选 ${result.candidates}，删除 ${result.deleted.length} 个目录，`
            + `跳过 ${result.skipped.length} 个（永久保留/未同步延长）`
            + `（释放 ${(result.freedBytes / 1024 ** 3).toFixed(2)} GiB，保留期 ${retentionDays} 天）`,
        )
        for (const error of result.errors) console.warn(`[output-cleanup] ${error}`)
      }
    } catch (error) {
      console.warn('[output-cleanup] 清理执行失败:', error)
    }
  }

  const bootTimer = setTimeout(() => void run('启动清理'), BOOT_DELAY_MS)
  const dailyTimer = setInterval(() => void run('每日清理'), DAILY_MS)
  // unref：不阻碍进程退出（dev 热更新 / 单测场景）
  bootTimer.unref()
  dailyTimer.unref()
  console.log(`[output-cleanup] 定时清理已启用：保留期 ${retentionDays} 天，每日一轮`)
})
