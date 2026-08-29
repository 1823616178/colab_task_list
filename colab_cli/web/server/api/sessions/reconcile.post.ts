// POST /api/sessions/reconcile（operator+，doc/02 §3.2）：手动触发一轮对账。
// 规则与写库见 services/sessionReconcile.ts（与启动/定时对账同一入口，并发合流）。
// Runner 不可达 → 503 RUNNER_UNAVAILABLE，本轮不动库。
import { requireRole } from '../../utils/auth'
import { sendRunnerError } from '../../utils/runner'
import { runSessionReconcile } from '../../services/sessionReconcile'

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  try {
    return await runSessionReconcile()
  } catch (error) {
    return sendRunnerError(event, error)
  }
})
