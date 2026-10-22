// GET /api/health：聚合 DB / Runner / 会话健康度（登录后可见，前端 30s 轮询驱动顶栏状态）。
import { In } from 'typeorm'
import { SessionSchema } from '../entities/Session'
import { TaskSchema } from '../entities/Task'
import { getDataSource } from '../utils/db'
import { checkRunnerHealth, runnerRequest } from '../services/runnerClient'
import { buildSessionViews, toActiveTaskSnapshots } from '../services/sessionReconcileRules'
import type { RunnerSessionSnapshot } from '../services/sessionReconcileRules'
import { aggregateHealth, OVERALL_LABELS, timedCheck } from '../services/health'
import type { HealthCheck } from '../services/health'

export default defineEventHandler(async () => {
  const [db, runner] = await Promise.all([
    timedCheck('数据库', async (): Promise<HealthCheck> => {
      await (await getDataSource()).query('SELECT 1')
      return { status: 'ok', message: '数据库连接正常' }
    }, 3_000),
    timedCheck('Runner', async (): Promise<HealthCheck> => {
      const health = await checkRunnerHealth()
      if (health.transport?.degraded) {
        return {
          status: 'degraded',
          message: `Runner 在线，传输已降级为 ${health.transport.mode}`,
          detail: { version: health.version, runningTasks: health.running_tasks },
        }
      }
      return {
        status: 'ok',
        message: `Runner 在线（${health.transport?.mode ?? '未知'} 通道）`,
        detail: { version: health.version, runningTasks: health.running_tasks },
      }
    }, 6_000),
  ])

  const sessions: HealthCheck = runner.status === 'down' || db.status === 'down'
    ? { status: 'degraded', message: 'Runner 或数据库不可用，无法核对会话' }
    : await timedCheck('会话', async (): Promise<HealthCheck> => {
      const runnerSessions = await runnerRequest<RunnerSessionSnapshot[]>('/sessions')
      const ds = await getDataSource()
      const views = buildSessionViews(
        runnerSessions,
        await ds.getRepository(SessionSchema).find(),
        toActiveTaskSnapshots(await ds.getRepository(TaskSchema).find({
          where: { status: In(['queued', 'running']) },
          select: { id: true, sessionName: true, status: true, workflowName: true },
        })),
      )
      const alive = views.filter(view => view.reachable).length
      const orphans = views.filter(view => view.isOrphan).map(view => view.name)
      return orphans.length > 0
        ? { status: 'degraded', message: `${orphans.length} 个孤儿 VM 正在计费`, detail: { alive, orphans } }
        : { status: 'ok', message: `在线 VM ${alive} 台`, detail: { alive, orphans } }
    }, 8_000)

  const checks = { db, runner, sessions }
  const status = aggregateHealth(checks)
  return { status, label: OVERALL_LABELS[status], checks, checkedAt: new Date().toISOString() }
})
