// GET /api/sessions（doc/02 §3.2，P4 聚合版）：
// Runner 实况（name/status/variant/accelerator/endpoint/reachable）
//   ⋈ sessions 表（expectedAlive/lastSeenAt/reconciledAt/createdAt…）
//   ⋈ tasks 表在途任务（runningTask/hasRunningTask）
// 合并规则在 services/sessionReconcileRules.buildSessionViews（纯函数）；
// 孤儿判定 isOrphan = reachable && !expectedAlive && !hasRunningTask（doc/04 §3.3）。
// Runner 不可达 → 503 RUNNER_UNAVAILABLE（前端横幅/页面降级不崩）。
import { In } from 'typeorm'
import { SessionSchema } from '../../entities/Session'
import { TaskSchema } from '../../entities/Task'
import { getDataSource } from '../../utils/db'
import { sendRunnerError } from '../../utils/runner'
import { runnerRequest } from '../../services/runnerClient'
import { buildSessionViews, toActiveTaskSnapshots } from '../../services/sessionReconcileRules'
import type {
  RunnerSessionSnapshot,
  SessionView,
} from '../../services/sessionReconcileRules'

/** Date → UTC ISO 串（doc/04 §4：接口全 UTC，展示层转本地） */
function iso(value: Date | null): string | null {
  return value ? value.toISOString() : null
}

function serialize(view: SessionView) {
  return {
    ...view,
    createdAt: iso(view.createdAt),
    lastSeenAt: iso(view.lastSeenAt),
    stoppedAt: iso(view.stoppedAt),
    reconciledAt: iso(view.reconciledAt),
  }
}

export default defineEventHandler(async (event) => {
  let runnerSessions: RunnerSessionSnapshot[]
  try {
    runnerSessions = await runnerRequest<RunnerSessionSnapshot[]>('/sessions')
  } catch (error) {
    return sendRunnerError(event, error)
  }
  const ds = await getDataSource()
  const dbRows = await ds.getRepository(SessionSchema).find()
  const activeTasks = toActiveTaskSnapshots(await ds.getRepository(TaskSchema).find({
    where: { status: In(['queued', 'running']) },
    select: { id: true, sessionName: true, status: true, workflowName: true },
  }))
  const items = buildSessionViews(runnerSessions, dbRows, activeTasks).map(serialize)
  return { items, generatedAt: new Date().toISOString() }
})
