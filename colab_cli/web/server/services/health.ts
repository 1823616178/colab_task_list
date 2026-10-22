// 系统健康聚合（纯函数部分，便于单测）：DB / Runner / 会话 三项检查 → 在线/降级/离线。
// 规则：
// - DB 不可用 → offline（控制台核心功能全部依赖 DB）；
// - Runner 不可用 → degraded（历史/下载仍可用，但无法提交与取消任务）；
// - 任一检查 degraded（传输降级 cli、存在孤儿 VM 等）→ degraded；
// - 其余 → online。

export type CheckStatus = 'ok' | 'degraded' | 'down'
export type OverallStatus = 'online' | 'degraded' | 'offline'

export interface HealthCheck {
  status: CheckStatus
  /** 面向用户的中文说明 */
  message: string
  latencyMs?: number
  detail?: Record<string, unknown>
}

export interface HealthChecks {
  db: HealthCheck
  runner: HealthCheck
  sessions: HealthCheck
}

export function aggregateHealth(checks: HealthChecks): OverallStatus {
  if (checks.db.status === 'down') return 'offline'
  const all = [checks.db, checks.runner, checks.sessions]
  if (all.some(check => check.status !== 'ok')) return 'degraded'
  return 'online'
}

export const OVERALL_LABELS: Record<OverallStatus, string> = {
  online: '在线',
  degraded: '降级',
  offline: '离线',
}

/** 给异步检查加超时与计时；抛错/超时统一转成 down */
export async function timedCheck(
  name: string,
  run: () => Promise<HealthCheck>,
  timeoutMs = 5_000,
): Promise<HealthCheck> {
  const started = Date.now()
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const result = await Promise.race([
      run(),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${name} 检查超时（${timeoutMs}ms）`)), timeoutMs)
      }),
    ])
    return { ...result, latencyMs: Date.now() - started }
  } catch (error) {
    return {
      status: 'down',
      message: error instanceof Error ? error.message : String(error),
      latencyMs: Date.now() - started,
    }
  } finally {
    if (timer) clearTimeout(timer)
  }
}
