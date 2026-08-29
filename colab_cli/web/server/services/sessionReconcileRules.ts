// 会话对账的纯规则层（doc/03 §3 对账规则）—— 不碰 DB、不碰 Runner、不碰 Nitro，
// 输入快照、输出视图与写计划，供 services/sessionReconcile.ts（编排）与
// api/sessions/index.get.ts（聚合只读视图）共用；单元测试见
// web/tests/sessionReconcileRules.test.ts（node --test 直跑，本模块零运行时依赖）。
//
// 「实际活/实际死」的口径（doc/03 只说了活/死，这里是落地定义）：
// - 实际活 := Runner 返回且 reachable=true（`colab sessions` 服务器端确认在计费）。
// - 实际死 := 不在 Runner 列表，或在列表但 reachable=false（本地状态文件还在、
//   服务器端已查无此 VM —— VM 被销毁/回收的典型签名）。
// Runner 列表里 reachable=false 且 DB 无行的条目只进视图（可见性），不落库
// （避免把「状态文件残留」固化成表行）。
import type { SessionAccelerator, SessionStatus } from '../entities/Session'

/** Runner GET /sessions 的条目（doc/02 §2.4） */
export interface RunnerSessionSnapshot {
  name: string
  /** IDLE | BUSY | UNKNOWN（大写） */
  status: string
  variant?: string | null
  accelerator?: string | null
  endpoint?: string | null
  reachable?: boolean
}

/**
 * 判断指定显示名是否已经对应一个存活 Runtime。
 * Runner 名称映射丢失时，用 sessions 表保存的 endpoint 兜底识别。
 */
export function findLiveSessionConflict(
  name: string,
  runnerSessions: RunnerSessionSnapshot[],
  storedEndpoint: string | null | undefined,
): RunnerSessionSnapshot | null {
  return runnerSessions.find(entry =>
    entry.reachable === true
    && (entry.name === name
      || (!!storedEndpoint && entry.endpoint === storedEndpoint))) ?? null
}

/** sessions 表行中对账所需的字段 */
export interface SessionRowSnapshot {
  name: string
  status: SessionStatus
  expectedAlive: boolean
  accelerator: SessionAccelerator | null
  variant: string | null
  endpoint: string | null
  createdAt: Date
  lastSeenAt: Date | null
  stoppedAt: Date | null
  reconciledAt: Date | null
}

/** tasks 表里的在途任务（queued/running） */
export interface ActiveTaskSnapshot {
  id: string
  sessionName: string
  status: 'queued' | 'running'
  workflowName: string
}

/**
 * 把 tasks 表查询结果收窄成 ActiveTaskSnapshot。
 * 查询侧已用 `status IN ('queued','running')` 过滤，但 TS 只看到宽泛的
 * TaskStatus；这里显式再过滤一次，既满足类型也兜住调用方漏写 where 的情况。
 */
export function toActiveTaskSnapshots(
  rows: Array<{ id: string, sessionName: string, status: string, workflowName: string }>,
): ActiveTaskSnapshot[] {
  return rows.flatMap(row =>
    row.status === 'queued' || row.status === 'running'
      ? [{
          id: row.id,
          sessionName: row.sessionName,
          status: row.status,
          workflowName: row.workflowName,
        }]
      : [],
  )
}

/** 某会话最近一次终态任务的 VM 保留信息（区分「正常销毁」与「异常失联」） */
export interface TerminalKeepSnapshot {
  keep: boolean
  sessionKept: boolean
}

/** status=recoverable 的历史任务；只有原 VM 仍存活时才允许继续显示为可重取。 */
export interface RecoverableTaskSnapshot {
  id: string
  sessionName: string
  currentPhase: string | null
}

/** VM 消失后将 recoverable 还原成其原始终态。 */
export interface ExpiredRecovery {
  taskId: string
  sessionName: string
  status: 'failed' | 'canceled'
}

/**
 * 找出已经失去恢复条件的任务。
 *
 * 名称映射还在时按 sessionName 判断；名称映射丢失时，使用 sessions 表保存的
 * endpoint 兜底。无法证明对应 Runtime 仍存活，就不能继续承诺“可恢复”。
 */
export function planExpiredRecoveries(
  runnerSessions: RunnerSessionSnapshot[],
  dbRows: SessionRowSnapshot[],
  recoverableTasks: RecoverableTaskSnapshot[],
): ExpiredRecovery[] {
  const dbByName = new Map(dbRows.map(row => [row.name, row]))
  return recoverableTasks.flatMap((task) => {
    const storedEndpoint = dbByName.get(task.sessionName)?.endpoint
    if (findLiveSessionConflict(task.sessionName, runnerSessions, storedEndpoint)) {
      return []
    }
    return [{
      taskId: task.id,
      sessionName: task.sessionName,
      status: task.currentPhase === 'task.canceled' ? 'canceled' as const : 'failed' as const,
    }]
  })
}

/** GET /api/sessions 的聚合条目：Runner 实况 ⋈ sessions 表 ⋈ 在途任务 */
export interface SessionView {
  name: string
  /** Runner 当前仍持有名称映射；false 时显示名仅来自 DB，停止必须走 endpoint。 */
  runnerNameKnown: boolean
  /** 服务器端确认活着（正在计费）；不在 Runner 列表或未确认均为 false */
  reachable: boolean
  /** Runner 实况 IDLE | BUSY | UNKNOWN；不在 Runner 列表为 null */
  runnerStatus: string | null
  /** A100 / L4 / TPU v6e-1（Runner 实况优先，回退 DB） */
  variant: string | null
  accelerator: string | null
  endpoint: string | null
  inDb: boolean
  dbStatus: SessionStatus | null
  expectedAlive: boolean
  /** 首次对账见到该 VM 的时间（Runner 不暴露 VM 创建时间，这是近似值） */
  createdAt: Date | null
  lastSeenAt: Date | null
  stoppedAt: Date | null
  reconciledAt: Date | null
  hasRunningTask: boolean
  runningTask: { id: string, workflowName: string, status: string } | null
  /** 孤儿（doc/04 §3.3）：reachable && !expectedAlive && !hasRunningTask —— 白烧计算单元 */
  isOrphan: boolean
}

export interface SessionUpsert {
  name: string
  status: SessionStatus
  expectedAlive: boolean
  accelerator: SessionAccelerator | null
  variant: string | null
  endpoint: string | null
  lastSeenAt: Date
}

/** 一轮对账要落库的写操作（由编排层执行） */
export interface ReconcilePlan {
  now: Date
  /** 实际活的会话：insert 或按 name 更新（lastSeenAt/endpoint/variant/expectedAlive） */
  upserts: SessionUpsert[]
  /** 实际死 & expectedAlive=true：正常销毁 → stopped；异常失联 → unreachable */
  markGone: { name: string, status: 'unreachable' | 'stopped' }[]
  /** 实际死 & expectedAlive=false & 表里还记着 idle/busy：补记 stopped */
  markStopped: string[]
  /** 实际死 & expectedAlive=true 波及的 queued/running 任务 → orphaned */
  tasksToOrphan: { taskId: string, sessionName: string }[]
  /** 本轮检出的孤儿会话名（reachable && !expectedAlive && 无 running task） */
  orphanNames: string[]
  /** 本轮确认活着的会话名 */
  aliveNames: string[]
}

/** Runner 条目键：名称为 "?"（名称映射丢失）时按 endpoint 区分，避免多个未映射 VM 撞键 */
function runnerKey(entry: RunnerSessionSnapshot): string {
  if (entry.name && entry.name !== '?') return entry.name
  return `endpoint:${entry.endpoint ?? ''}`
}

function normalizeAccelerator(value: string | null | undefined): SessionAccelerator | null {
  return value === 'gpu' || value === 'tpu' || value === 'cpu' ? value : null
}

/** 视图用的活跃任务匹配：优先 running，其次 queued（同 session 互斥，至多一条） */
function pickSessionTask(
  name: string,
  activeTasks: ActiveTaskSnapshot[],
): ActiveTaskSnapshot | null {
  return activeTasks.find(t => t.sessionName === name && t.status === 'running')
    ?? activeTasks.find(t => t.sessionName === name)
    ?? null
}

/**
 * 合并 Runner 实况 + sessions 表 + 在途任务为展示视图（GET /api/sessions 的形状）。
 * 排序：孤儿置顶 → 活着的 → 其余按名字。
 */
export function buildSessionViews(
  runnerSessions: RunnerSessionSnapshot[],
  dbRows: SessionRowSnapshot[],
  activeTasks: ActiveTaskSnapshot[],
): SessionView[] {
  const dbByName = new Map(dbRows.map(row => [row.name, row]))
  const matchedDb = new Set<string>()
  const views: SessionView[] = []

  for (const entry of runnerSessions) {
    const key = runnerKey(entry)
    // 名称映射丢失（"?"）的条目按 endpoint 找回 DB 行
    const dbRow = dbByName.get(key)
      ?? dbRows.find(row => !matchedDb.has(row.name) && !!entry.endpoint && row.endpoint === entry.endpoint)
      ?? null
    if (dbRow) matchedDb.add(dbRow.name)
    const name = dbRow?.name ?? (entry.name && entry.name !== '?' ? entry.name : `? (${entry.endpoint ?? '未知 endpoint'})`)
    const task = dbRow ? pickSessionTask(dbRow.name, activeTasks) : null
    const reachable = entry.reachable === true
    const expectedAlive = dbRow?.expectedAlive ?? false
    const hasRunningTask = task?.status === 'running'
    views.push({
      name,
      runnerNameKnown: !!entry.name && entry.name !== '?',
      reachable,
      runnerStatus: entry.status || null,
      variant: entry.variant ?? dbRow?.variant ?? null,
      accelerator: entry.accelerator ?? dbRow?.accelerator ?? null,
      endpoint: entry.endpoint ?? dbRow?.endpoint ?? null,
      inDb: dbRow !== null,
      dbStatus: dbRow?.status ?? null,
      expectedAlive,
      createdAt: dbRow?.createdAt ?? null,
      lastSeenAt: dbRow?.lastSeenAt ?? null,
      stoppedAt: dbRow?.stoppedAt ?? null,
      reconciledAt: dbRow?.reconciledAt ?? null,
      hasRunningTask,
      runningTask: task ? { id: task.id, workflowName: task.workflowName, status: task.status } : null,
      isOrphan: reachable && !expectedAlive && !hasRunningTask,
    })
  }

  // Runner 列表外的 DB 行（stopped/unreachable 历史、或 Runner 暂时看不到的）
  for (const row of dbRows) {
    if (matchedDb.has(row.name)) continue
    const task = pickSessionTask(row.name, activeTasks)
    views.push({
      name: row.name,
      runnerNameKnown: false,
      reachable: false,
      runnerStatus: null,
      variant: row.variant,
      accelerator: row.accelerator,
      endpoint: row.endpoint,
      inDb: true,
      dbStatus: row.status,
      expectedAlive: row.expectedAlive,
      createdAt: row.createdAt,
      lastSeenAt: row.lastSeenAt,
      stoppedAt: row.stoppedAt,
      reconciledAt: row.reconciledAt,
      hasRunningTask: task?.status === 'running',
      runningTask: task ? { id: task.id, workflowName: task.workflowName, status: task.status } : null,
      isOrphan: false,
    })
  }

  // 在途任务指向的会话既不在 Runner 也不在 DB（如 dry_run 不建 VM）：合成一行保证可见
  for (const task of activeTasks) {
    if (views.some(view => view.runningTask?.id === task.id)) continue
    if (views.some(view => view.name === task.sessionName)) continue
    views.push({
      name: task.sessionName,
      runnerNameKnown: false,
      reachable: false,
      runnerStatus: null,
      variant: null,
      accelerator: null,
      endpoint: null,
      inDb: false,
      dbStatus: null,
      expectedAlive: false,
      createdAt: null,
      lastSeenAt: null,
      stoppedAt: null,
      reconciledAt: null,
      hasRunningTask: task.status === 'running',
      runningTask: { id: task.id, workflowName: task.workflowName, status: task.status },
      isOrphan: false,
    })
  }

  views.sort((a, b) =>
    Number(b.isOrphan) - Number(a.isOrphan)
    || Number(b.reachable) - Number(a.reachable)
    || a.name.localeCompare(b.name))
  return views
}

/**
 * 对账规则（doc/03 §3）：
 *   实际活 & expectedAlive=false & 无 running task → 孤儿（orphanNames，告警 + 一键停止）
 *   实际死 & expectedAlive=true                   → unreachable，queued/running 任务转 orphaned
 *   实际活 & 有在途任务                            → 正常：更新 lastSeenAt，expectedAlive 置真
 * latestTerminalBySession 提供最近一次终态任务的 keep/sessionKept：
 * 实际死但最后一次任务本就没保留 VM（keep=false 且未触发失败保护）→ 记 stopped 而非
 * unreachable（正常销毁，不是事故）。
 */
export function planSessionReconciliation(
  runnerSessions: RunnerSessionSnapshot[],
  dbRows: SessionRowSnapshot[],
  activeTasks: ActiveTaskSnapshot[],
  latestTerminalBySession: ReadonlyMap<string, TerminalKeepSnapshot>,
  now: Date,
): ReconcilePlan {
  const dbByName = new Map(dbRows.map(row => [row.name, row]))
  const plan: ReconcilePlan = {
    now,
    upserts: [],
    markGone: [],
    markStopped: [],
    tasksToOrphan: [],
    orphanNames: [],
    aliveNames: [],
  }

  for (const entry of runnerSessions) {
    if (entry.reachable !== true) continue // 未经服务器确认：只进视图，不落库
    const key = runnerKey(entry)
    const dbRow = dbByName.get(key)
      ?? dbRows.find(row => !!entry.endpoint && row.endpoint === entry.endpoint)
      ?? null
    // 名称映射丢失（"?"）无法以 name 为键落库：只报孤儿，由人工按 endpoint 停止
    if (!dbRow && key.startsWith('endpoint:')) {
      plan.orphanNames.push(`? (${entry.endpoint ?? '未知 endpoint'})`)
      plan.aliveNames.push(key)
      continue
    }
    const name = dbRow?.name ?? key
    const hasActiveTask = activeTasks.some(t => t.sessionName === name)
    const hasRunningTask = activeTasks.some(t => t.sessionName === name && t.status === 'running')
    // 有在途任务（含 queued：VM 可能刚建、task.start 未达）→ 我们认为它该活着
    const expectedAlive = (dbRow?.expectedAlive ?? false) || hasActiveTask
    plan.upserts.push({
      name,
      status: entry.status === 'BUSY' ? 'busy' : 'idle',
      expectedAlive,
      accelerator: normalizeAccelerator(entry.accelerator) ?? dbRow?.accelerator ?? null,
      variant: entry.variant ?? dbRow?.variant ?? null,
      endpoint: entry.endpoint ?? dbRow?.endpoint ?? null,
      lastSeenAt: now,
    })
    plan.aliveNames.push(name)
    if (!expectedAlive && !hasRunningTask) plan.orphanNames.push(name)
  }

  for (const row of dbRows) {
    const entry = runnerSessions.find((candidate) => {
      if (candidate.reachable !== true) return false
      return runnerKey(candidate) === row.name
        || (!!candidate.endpoint && candidate.endpoint === row.endpoint)
    })
    if (entry) continue // 实际活，上面已处理
    // ── 实际死 ──────────────────────────────────────────────
    if (row.expectedAlive) {
      const inflight = activeTasks.filter(t => t.sessionName === row.name)
      for (const task of inflight) {
        plan.tasksToOrphan.push({ taskId: task.id, sessionName: row.name })
      }
      const lastTerminal = latestTerminalBySession.get(row.name)
      const cleanStop = inflight.length === 0
        && !!lastTerminal && !lastTerminal.keep && !lastTerminal.sessionKept
      plan.markGone.push({ name: row.name, status: cleanStop ? 'stopped' : 'unreachable' })
    } else if (row.status === 'idle' || row.status === 'busy') {
      // 曾见过活着、现已消失且无人期待（如孤儿 VM 自行过期）→ 补记 stopped
      plan.markStopped.push(row.name)
    }
    // status 已是 unreachable/stopped：保持，仅由编排层统一回写 reconciledAt
  }
  return plan
}
