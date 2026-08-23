// 过期 refresh_tokens 定时清理（doc/03 §2 索引 `(expiresAt)`、web/README 遗留）。
// 只删 expiresAt < now：未过期但已撤销的行必须留着，否则重用检测会漏。
// 启动 30 秒后跑一轮（覆盖停机窗口），之后每 24 小时一轮；失败只告警。
import { purgeExpiredRefreshTokens } from '../utils/token'

const BOOT_DELAY_MS = 30_000
const DAILY_MS = 24 * 60 * 60 * 1000

declare global {
  var __colabRefreshCleanupTimer: ReturnType<typeof setInterval> | undefined
}

export default defineNitroPlugin(() => {
  if (globalThis.__colabRefreshCleanupTimer) {
    return
  }

  const run = async (label: string) => {
    try {
      const deleted = await purgeExpiredRefreshTokens()
      if (deleted > 0) {
        console.log(`[refresh-cleanup] ${label}：删除 ${deleted} 行过期 refresh_tokens`)
      }
    } catch (error) {
      console.warn('[refresh-cleanup] 清理失败:', error)
    }
  }

  const bootTimer = setTimeout(() => void run('启动清理'), BOOT_DELAY_MS)
  globalThis.__colabRefreshCleanupTimer = setInterval(() => void run('每日清理'), DAILY_MS)
  bootTimer.unref()
  globalThis.__colabRefreshCleanupTimer.unref?.()
})
