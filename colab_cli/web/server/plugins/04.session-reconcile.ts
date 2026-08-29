// P4 会话对账（doc/03 §3、doc/05 P4）：启动即对账 + 每 5 分钟定时对账。
// - 防多实例/重复注册：globalThis 标记（dev 热更新会重跑插件；与 db.ts 同套路），
//   旧实例的定时器继续持有旧闭包，不叠加。
// - Runner 不可达 → runSessionReconcile 抛 RunnerUnavailableError：记告警跳过本轮，
//   绝不在没有实况时动 sessions/tasks 表。
// - 与手动 POST /api/sessions/reconcile 的并发由服务内互斥锁合流。
import { runSessionReconcile } from '../services/sessionReconcile'

const RECONCILE_INTERVAL_MS = 5 * 60 * 1000

declare global {
  var __colabSessionReconcileTimer: ReturnType<typeof setInterval> | undefined
}

export default defineNitroPlugin(() => {
  if (globalThis.__colabSessionReconcileTimer) {
    return // 已注册（dev 热更新重跑插件）
  }

  const tick = (label: string) => {
    void runSessionReconcile().catch((error: unknown) => {
      console.warn(
        `[session-reconcile] ${label}对账失败（Runner 未启动？）:`,
        error instanceof Error ? error.message : error,
      )
    })
  }

  tick('启动')
  globalThis.__colabSessionReconcileTimer = setInterval(() => tick('定时'), RECONCILE_INTERVAL_MS)
  // 不阻止进程退出（单测/脚本场景）
  globalThis.__colabSessionReconcileTimer.unref?.()
})
