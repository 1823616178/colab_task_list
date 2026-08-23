// DELETE /api/sessions/by-endpoint/:endpoint（operator+，doc/02 §3.2）：
// 转发 Runner DELETE /sessions/by-endpoint/{ep}（包装 stop_unmapped_endpoint()），
// 覆盖「名称映射丢失」（Runner 侧名称为 "?"）只能按 endpoint 停止的场景。
// 成功后回写 sessions 表行（按 endpoint 匹配）：stopped / expectedAlive=false。
import { getRouterParam } from 'h3'
import { requireRole } from '../../../utils/auth'
import { sendApiError } from '../../../utils/errors'
import { sendRunnerError } from '../../../utils/runner'
import { runnerRequest } from '../../../services/runnerClient'
import { markSessionStopped } from '../../../services/sessionReconcile'

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  const endpoint = getRouterParam(event, 'endpoint')!
  if (!endpoint) {
    return sendApiError(event, 400, 'BAD_REQUEST', 'endpoint 不能为空')
  }
  try {
    // endpoint 含特殊字符（Runner 路由为 {endpoint:path}），encodeURIComponent 透传
    const result = await runnerRequest<{ ok: boolean, endpoint: string }>(
      `/sessions/by-endpoint/${encodeURIComponent(endpoint)}`,
      { method: 'DELETE' },
    )
    // DB 回写失败只告警，不掩盖「VM 已停止」的结果
    await markSessionStopped({ endpoint }).catch((error: unknown) => {
      console.warn('[session-reconcile] 按 endpoint 停止后回写 sessions 表失败:', error)
    })
    return result
  } catch (error) {
    return sendRunnerError(event, error)
  }
})
