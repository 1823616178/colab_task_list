// 会话对账编排（doc/03 §3 对账规则、doc/05 P4）：
// 拉 Runner GET /sessions 实况 → 与 sessions 表 + tasks 在途任务 join →
// 经 sessionReconcileRules.planSessionReconciliation（纯函数，规则全在那里）算出写计划
// → 落库：活会话 upsert（lastSeenAt/expectedAlive）、失联会话 unreachable/stopped、
// 波及任务转 orphaned 并取消其 SSE 订阅（taskSubscription.stopTaskSubscription）、
// 全表回写 reconciledAt。
//
// 触发方三处：plugins/04.session-reconcile.ts（启动即对账 + 每 5 分钟）、
// POST /api/sessions/reconcile（手动，operator+）。并发调用合流为同一轮（in-flight
// promise 共享），避免定时与手动重叠写库。
//
// Runner 不可达：抛 RunnerUnavailableError —— 插件记告警跳过本轮，API 层映射 503。
// 宁缺勿滥：Runner 给不出实况时绝不动 sessions/tasks 表（空列表也是合法实况：
// 一台 VM 都没有）。
import { In } from 'typeorm'
import type { QueryDeepPartialEntity } from 'typeorm'
import { SessionSchema } from '../entities/Session'
import type { Session } from '../entities/Session'
import { TaskSchema, TERMINAL_TASK_STATUSES } from '../entities/Task'
import { getDataSource } from '../utils/db'
import { runnerRequest } from './runnerClient'
import { stopTaskSubscription } from './taskSubscription'
import {
  planExpiredRecoveries,
  planSessionReconciliation,
  toActiveTaskSnapshots,
} from './sessionReconcileRules'
import type {
  RecoverableTaskSnapshot,
  RunnerSessionSnapshot,
  SessionRowSnapshot,
  TerminalKeepSnapshot,
} from './sessionReconcileRules'

export interface ReconcileSummary {
  ok: true
  /** 本轮对账时间（UTC ISO） */
  reconciledAt: string
  /** 服务器端确认活着的会话 */
  alive: string[]
  /** 孤儿：reachable && !expectedAlive && 无 running task（白烧计算单元） */
  orphans: string[]
  /** 实际死 & expectedAlive=true：标记 unreachable/stopped 的会话 */
  gone: { name: string, status: 'unreachable' | 'stopped' }[]
  /** 实际死 & expectedAlive=false：补记 stopped 的会话 */
  stopped: string[]
  /** 被转 orphaned 的任务 */
  orphanedTasks: { taskId: string, sessionName: string }[]
  /** VM 已不存在、由 recoverable 还原成失败/取消的任务 */
  expiredRecoveries: { taskId: string, sessionName: string, status: 'failed' | 'canceled' }[]
}

export interface ReconcileDeps {
  /** 默认 runnerRequest('/sessions')；测试/演练可注入假快照 */
  fetchRunnerSessions?: () => Promise<RunnerSessionSnapshot[]>
  now?: Date
}

// globalThis 缓存：dev 热更新时对账互斥锁不丢（与 db.ts 同套路）
declare global {
  var __colabSessionReconcileRunning: Promise<ReconcileSummary> | undefined
}

/** 触发一轮对账；并发调用共享同一轮结果（幂等互斥）。 */
export function runSessionReconcile(deps: ReconcileDeps = {}): Promise<ReconcileSummary> {
  if (globalThis.__colabSessionReconcileRunning) {
    return globalThis.__colabSessionReconcileRunning
  }
  const run = doReconcile(deps).finally(() => {
    if (globalThis.__colabSessionReconcileRunning === run) {
      globalThis.__colabSessionReconcileRunning = undefined
    }
  })
  globalThis.__colabSessionReconcileRunning = run
  return run
}

async function doReconcile(deps: ReconcileDeps): Promise<ReconcileSummary> {
  const now = deps.now ?? new Date()
  const fetchRunner = deps.fetchRunnerSessions
    ?? (() => runnerRequest<RunnerSessionSnapshot[]>('/sessions'))
  // Runner 实况拉不到 → 直接抛（RUNNER_UNAVAILABLE），本轮不动库
  const runnerSessions = await fetchRunner()

  const ds = await getDataSource()
  const sessionRepo = ds.getRepository(SessionSchema)
  const dbRows: SessionRowSnapshot[] = await sessionRepo.find()
  const activeTasks = toActiveTaskSnapshots(await ds.getRepository(TaskSchema).find({
    where: { status: In(['queued', 'running']) },
    select: { id: true, sessionName: true, status: true, workflowName: true },
  }))
  const recoverableTasks: RecoverableTaskSnapshot[] = await ds.getRepository(TaskSchema).find({
    where: { status: 'recoverable' },
    select: { id: true, sessionName: true, currentPhase: true },
  })

  // 「正常销毁 or 异常失联」判定材料：expectedAlive=true 会话的最近一次终态任务
  const latestTerminalBySession = new Map<string, TerminalKeepSnapshot>()
  for (const row of dbRows) {
    if (!row.expectedAlive) continue
    const last = await ds.getRepository(TaskSchema).findOne({
      where: { sessionName: row.name, status: In(TERMINAL_TASK_STATUSES) },
      order: { finishedAt: 'DESC' },
      select: { keep: true, sessionKept: true },
    })
    if (last) latestTerminalBySession.set(row.name, { keep: last.keep, sessionKept: last.sessionKept })
  }

  const plan = planSessionReconciliation(
    runnerSessions, dbRows, activeTasks, latestTerminalBySession, now,
  )
  const expiredRecoveries = planExpiredRecoveries(runnerSessions, dbRows, recoverableTasks)

  // ── 落库：活会话 upsert（createdAt 仅 insert 时写 = 首次见到的时间，近似 VM 创建时间）──
  for (const upsert of plan.upserts) {
    await ds.query(
      `INSERT INTO \`sessions\`
         (\`name\`, \`status\`, \`expectedAlive\`, \`accelerator\`, \`variant\`, \`endpoint\`,
          \`createdAt\`, \`lastSeenAt\`, \`stoppedAt\`, \`reconciledAt\`)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
       ON DUPLICATE KEY UPDATE
         \`status\` = VALUES(\`status\`),
         \`expectedAlive\` = VALUES(\`expectedAlive\`),
         \`accelerator\` = VALUES(\`accelerator\`),
         \`variant\` = VALUES(\`variant\`),
         \`endpoint\` = VALUES(\`endpoint\`),
         \`lastSeenAt\` = VALUES(\`lastSeenAt\`),
         \`stoppedAt\` = NULL,
         \`reconciledAt\` = VALUES(\`reconciledAt\`)`,
      [
        upsert.name, upsert.status, upsert.expectedAlive ? 1 : 0,
        upsert.accelerator, upsert.variant, upsert.endpoint,
        now, upsert.lastSeenAt, now,
      ],
    )
  }

  // ── 实际死 & expectedAlive=true → unreachable（或正常销毁 → stopped），不再期待它活 ──
  for (const gone of plan.markGone) {
    const updates: QueryDeepPartialEntity<Session> = {
      status: gone.status,
      expectedAlive: false,
      reconciledAt: now,
    }
    if (gone.status === 'stopped') updates.stoppedAt = now
    await ds.getRepository(SessionSchema).update({ name: gone.name }, updates)
  }

  // ── 实际死 & expectedAlive=false & 表里还记着 idle/busy → 补记 stopped ──
  for (const name of plan.markStopped) {
    await ds.getRepository(SessionSchema).update(
      { name },
      { status: 'stopped', stoppedAt: now, reconciledAt: now },
    )
  }

  // ── 波及任务转 orphaned（doc/03 §4 状态机）；先取消 SSE 订阅再落库 ──
  for (const orphan of plan.tasksToOrphan) {
    stopTaskSubscription(orphan.taskId)
    // WHERE 限定仍在途：与 SSE 终态回写并发时不覆盖已终态的行
    const result = await ds.getRepository(TaskSchema).update(
      { id: orphan.taskId, status: In(['queued', 'running']) },
      {
        status: 'orphaned',
        finishedAt: now,
        errorCode: 'SESSION_LOST',
        errorMessage: `对账发现会话 ${orphan.sessionName} 的 VM 已消失，任务状态不可知`,
      },
    )
    if (result.affected) {
      console.warn(`[session-reconcile] 任务 ${orphan.taskId}（${orphan.sessionName}）转 orphaned：VM 已消失`)
    }
  }

  // ── recoverable 只在对应 VM 仍存活时成立；VM 消失后还原原始终态 ──
  for (const recovery of expiredRecoveries) {
    await ds.getRepository(TaskSchema).update(
      { id: recovery.taskId, status: 'recoverable' },
      { status: recovery.status, sessionKept: false },
    )
  }

  // ── 全表回写 reconciledAt（本轮所有行都被审视过）──
  await ds.query('UPDATE `sessions` SET `reconciledAt` = ?', [now])

  const summary: ReconcileSummary = {
    ok: true,
    reconciledAt: now.toISOString(),
    alive: plan.aliveNames,
    orphans: plan.orphanNames,
    gone: plan.markGone,
    stopped: plan.markStopped,
    orphanedTasks: plan.tasksToOrphan,
    expiredRecoveries,
  }
  console.log(
    `[session-reconcile] 本轮对账完成: 活 ${summary.alive.length}，孤儿 ${summary.orphans.length}`
      + `，失联 ${plan.markGone.filter(g => g.status === 'unreachable').length}`
      + `，转 orphaned 任务 ${summary.orphanedTasks.length}`
      + `，失效恢复项 ${summary.expiredRecoveries.length}`,
  )
  if (summary.orphans.length > 0) {
    console.warn(`[session-reconcile] ⚠️ 检出孤儿会话（白烧计算单元）: ${summary.orphans.join(', ')}`)
  }
  return summary
}

/** 停止会话成功后回写 DB 行（DELETE /api/sessions/:name 与 by-endpoint 共用）。 */
export async function markSessionStopped(
  criteria: { name?: string, endpoint?: string },
  now: Date = new Date(),
): Promise<void> {
  const ds = await getDataSource()
  const where = criteria.name ? { name: criteria.name } : { endpoint: criteria.endpoint! }
  const matchedRows = await ds.getRepository(SessionSchema).find({
    where,
    select: { name: true },
  })
  await ds.getRepository(SessionSchema).update(where, {
    status: 'stopped',
    expectedAlive: false,
    stoppedAt: now,
    reconciledAt: now,
  })
  const names = new Set(matchedRows.map(row => row.name))
  if (criteria.name) names.add(criteria.name)
  if (names.size === 0) return

  const recoverableTasks = await ds.getRepository(TaskSchema).find({
    where: { status: 'recoverable', sessionName: In([...names]) },
    select: { id: true, currentPhase: true },
  })
  for (const task of recoverableTasks) {
    await ds.getRepository(TaskSchema).update(
      { id: task.id, status: 'recoverable' },
      {
        status: task.currentPhase === 'task.canceled' ? 'canceled' : 'failed',
        sessionKept: false,
      },
    )
  }
}
