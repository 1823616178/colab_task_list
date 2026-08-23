// DELETE /api/sessions/:name（operator+，doc/02 §3.2）：转发 Runner 停止会话
// （包装 stop_named_session()；同时解除 Runner 侧恢复锁）。
// P4：停止成功后回写 sessions 表（status=stopped / expectedAlive=false / stoppedAt），
// 让对账与孤儿检测立刻知情；DB 回写失败只告警，不掩盖「VM 已停止」的结果。
import { getRouterParam } from 'h3'
import { requireRole } from '../../utils/auth'
import { sendApiError } from '../../utils/errors'
import { sendRunnerError } from '../../utils/runner'
import { RunnerApiError, runnerRequest } from '../../services/runnerClient'
import { markSessionStopped } from '../../services/sessionReconcile'
import { getDataSource } from '../../utils/db'
import { SessionSchema } from '../../entities/Session'

const NAME_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  const name = getRouterParam(event, 'name')!
  if (!NAME_PATTERN.test(name)) {
    return sendApiError(event, 400, 'BAD_REQUEST', `会话名非法: ${name}`)
  }
  try {
    const result = await runnerRequest<{ ok: boolean, name: string }>(
      `/sessions/${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    )
    await markSessionStopped({ name }).catch((error: unknown) => {
      console.warn(`[session-reconcile] 停止后回写 sessions 表失败（${name}）:`, error)
    })
    return result
  } catch (error) {
    // 名称映射被新版 colab CLI 清掉后，云端 Runtime 仍可能存活。此时用 DB
    // 保存的 endpoint 自动兜底，兼容旧页面和直接调用按名称停止的客户端。
    if (error instanceof RunnerApiError
      && error.code === 'SESSION_STOP_FAILED'
      && /not found|不存在/i.test(error.message)) {
      const ds = await getDataSource()
      const stored = await ds.getRepository(SessionSchema).findOne({
        where: { name },
        select: { endpoint: true },
      })
      if (stored?.endpoint) {
        try {
          const fallback = await runnerRequest<{ ok: boolean, endpoint: string }>(
            `/sessions/by-endpoint/${encodeURIComponent(stored.endpoint)}`,
            { method: 'DELETE' },
          )
          await markSessionStopped({ name })
          return {
            ...fallback,
            name,
            log: `名称映射已丢失，已按 Runtime endpoint 停止: ${stored.endpoint}`,
          }
        } catch (fallbackError) {
          return sendRunnerError(event, fallbackError)
        }
      }
    }
    return sendRunnerError(event, error)
  }
})
