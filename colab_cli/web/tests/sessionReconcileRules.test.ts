// 会话对账规则单元测试（doc/03 §3 三条规则 + 边界）。
// 运行：cd web && npm test（= node --test "tests/*.test.ts"；Node ≥23.6
// 原生 strip-types 直跑 TS。注意目录参数 `node --test tests/` 在 Node 26 已失效；
// 被测模块 server/services/sessionReconcileRules.ts 零运行时依赖，不碰 DB/Nitro）。
// Runner 的 sessions 响应在这里就是普通数组参数 —— 即「mock runnerClient」的方式。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildSessionViews,
  findLiveSessionConflict,
  planExpiredRecoveries,
  planSessionReconciliation,
} from '../server/services/sessionReconcileRules.ts'
import type {
  ActiveTaskSnapshot,
  RunnerSessionSnapshot,
  SessionRowSnapshot,
  TerminalKeepSnapshot,
} from '../server/services/sessionReconcileRules.ts'

const NOW = new Date('2026-08-22T14:00:00.000Z')

function runnerSession(partial: Partial<RunnerSessionSnapshot> & { name: string }): RunnerSessionSnapshot {
  return {
    status: 'IDLE',
    variant: 'L4',
    accelerator: 'gpu',
    endpoint: `ep-${partial.name}`,
    reachable: true,
    ...partial,
  }
}

function dbRow(partial: Partial<SessionRowSnapshot> & { name: string }): SessionRowSnapshot {
  return {
    status: 'idle',
    expectedAlive: false,
    accelerator: 'gpu',
    variant: 'L4',
    endpoint: `ep-${partial.name}`,
    createdAt: new Date('2026-08-22T10:00:00.000Z'),
    lastSeenAt: null,
    stoppedAt: null,
    reconciledAt: null,
    ...partial,
  }
}

function task(partial: Partial<ActiveTaskSnapshot> & { id: string, sessionName: string }): ActiveTaskSnapshot {
  return { status: 'running', workflowName: '测试工作流', ...partial }
}

function plan(
  runner: RunnerSessionSnapshot[],
  db: SessionRowSnapshot[],
  active: ActiveTaskSnapshot[] = [],
  terminal: ReadonlyMap<string, TerminalKeepSnapshot> = new Map(),
) {
  return planSessionReconciliation(runner, db, active, terminal, NOW)
}

// ── 规则一：实际活 & expectedAlive=false & 无 running task → 孤儿 ──────────
test('规则一：活会话无人认领且无 running 任务 → 孤儿告警，可一键停止', () => {
  const result = plan(
    [runnerSession({ name: 'ghost' })],
    [dbRow({ name: 'ghost', expectedAlive: false })],
  )
  assert.deepEqual(result.orphanNames, ['ghost'])
  assert.deepEqual(result.tasksToOrphan, [])
  // 活会话照常 upsert（刷新 lastSeenAt），但 expectedAlive 保持 false
  const upsert = result.upserts.find(u => u.name === 'ghost')
  assert.ok(upsert)
  assert.equal(upsert.expectedAlive, false)
  assert.deepEqual(upsert.lastSeenAt, NOW)

  const view = buildSessionViews(
    [runnerSession({ name: 'ghost' })],
    [dbRow({ name: 'ghost', expectedAlive: false })],
    [],
  )
  assert.equal(view[0]!.isOrphan, true)
  assert.equal(view[0]!.reachable, true)
  assert.equal(view[0]!.runnerNameKnown, true)
})

test('同名创建保护：名称丢失时仍按 DB endpoint 识别存活 VM', () => {
  const sessions = [runnerSession({ name: '?', endpoint: 'ep-old' })]
  assert.equal(
    findLiveSessionConflict('comfyui-h3', sessions, 'ep-old')?.endpoint,
    'ep-old',
  )
  assert.equal(findLiveSessionConflict('another', sessions, 'ep-other'), null)
})

test('恢复状态：对应 VM 消失后按终态来源还原为失败或已取消', () => {
  const expired = planExpiredRecoveries(
    [],
    [dbRow({ name: 'failed-vm' }), dbRow({ name: 'canceled-vm' })],
    [
      { id: 't-failed', sessionName: 'failed-vm', currentPhase: 'task.failed' },
      { id: 't-canceled', sessionName: 'canceled-vm', currentPhase: 'task.canceled' },
    ],
  )
  assert.deepEqual(expired, [
    { taskId: 't-failed', sessionName: 'failed-vm', status: 'failed' },
    { taskId: 't-canceled', sessionName: 'canceled-vm', status: 'canceled' },
  ])
})

test('恢复状态：同名 VM 或 DB endpoint 对应的未映射 VM 存活时仍可重取', () => {
  const expired = planExpiredRecoveries(
    [
      runnerSession({ name: 'named-vm', endpoint: 'ep-named' }),
      runnerSession({ name: '?', endpoint: 'ep-unmapped' }),
    ],
    [dbRow({ name: 'mapped-vm', endpoint: 'ep-unmapped' })],
    [
      { id: 't-named', sessionName: 'named-vm', currentPhase: 'task.failed' },
      { id: 't-mapped', sessionName: 'mapped-vm', currentPhase: 'task.failed' },
    ],
  )
  assert.deepEqual(expired, [])
})

// ── 规则二：实际死 & expectedAlive=true → unreachable + 任务转 orphaned ────
test('规则二：死会话仍被期待 → unreachable，running/queued 任务转 orphaned', () => {
  const result = plan(
    [], // Runner 列表为空：VM 已消失
    [dbRow({ name: 'lost-vm', expectedAlive: true, status: 'busy' })],
    [
      task({ id: 't-run', sessionName: 'lost-vm', status: 'running' }),
      task({ id: 't-other', sessionName: 'someone-else', status: 'running' }),
    ],
  )
  assert.deepEqual(result.markGone, [{ name: 'lost-vm', status: 'unreachable' }])
  // 只波及该会话的在途任务
  assert.deepEqual(result.tasksToOrphan, [{ taskId: 't-run', sessionName: 'lost-vm' }])
  assert.deepEqual(result.orphanNames, [])
})

test('规则二补充：queued 任务同样被波及', () => {
  const result = plan(
    [],
    [dbRow({ name: 'lost-vm', expectedAlive: true })],
    [task({ id: 't-queued', sessionName: 'lost-vm', status: 'queued' })],
  )
  assert.deepEqual(result.tasksToOrphan, [{ taskId: 't-queued', sessionName: 'lost-vm' }])
  assert.deepEqual(result.markGone, [{ name: 'lost-vm', status: 'unreachable' }])
})

test('规则二边界：reachable=false（本地状态文件残留）也按「实际死」处理', () => {
  const result = plan(
    [runnerSession({ name: 'half-gone', reachable: false, status: 'UNKNOWN' })],
    [dbRow({ name: 'half-gone', expectedAlive: true })],
    [task({ id: 't-x', sessionName: 'half-gone' })],
  )
  assert.deepEqual(result.markGone, [{ name: 'half-gone', status: 'unreachable' }])
  assert.deepEqual(result.tasksToOrphan, [{ taskId: 't-x', sessionName: 'half-gone' }])
  // 未确认活的条目不落库
  assert.deepEqual(result.upserts, [])
})

test('规则二细化：无在途任务且最近任务 keep=false 未保留 VM → 正常销毁记 stopped', () => {
  const terminal = new Map<string, TerminalKeepSnapshot>([
    ['done-vm', { keep: false, sessionKept: false }],
    ['kept-vm', { keep: true, sessionKept: false }],
  ])
  const result = plan(
    [],
    [
      dbRow({ name: 'done-vm', expectedAlive: true }),
      dbRow({ name: 'kept-vm', expectedAlive: true }),
    ],
    [],
    terminal,
  )
  assert.deepEqual(result.markGone, [
    { name: 'done-vm', status: 'stopped' },
    { name: 'kept-vm', status: 'unreachable' },
  ])
})

// ── 规则三：实际活 & 有在途任务 → 正常，更新 lastSeenAt、expectedAlive 置真 ──
test('规则三：活会话有 running 任务 → 非孤儿，expectedAlive 置真并刷 lastSeenAt', () => {
  const result = plan(
    [runnerSession({ name: 'busy-vm', status: 'BUSY', variant: 'A100' })],
    [dbRow({ name: 'busy-vm', expectedAlive: false })],
    [task({ id: 't-run', sessionName: 'busy-vm', status: 'running' })],
  )
  assert.deepEqual(result.orphanNames, [])
  const upsert = result.upserts.find(u => u.name === 'busy-vm')
  assert.ok(upsert)
  assert.equal(upsert.status, 'busy')
  assert.equal(upsert.expectedAlive, true)
  assert.equal(upsert.variant, 'A100')
  assert.deepEqual(upsert.lastSeenAt, NOW)
})

test('规则三边界：仅 queued 任务（VM 刚建、task.start 未达）也视为该活着，不报孤儿', () => {
  const result = plan(
    [runnerSession({ name: 'starting-vm' })],
    [dbRow({ name: 'starting-vm', expectedAlive: false })],
    [task({ id: 't-q', sessionName: 'starting-vm', status: 'queued' })],
  )
  assert.deepEqual(result.orphanNames, [])
  assert.equal(result.upserts[0]!.expectedAlive, true)
})

// ── 其余边界 ─────────────────────────────────────────────────
test('活会话首次出现（DB 无行）→ insert 计划；无任务即孤儿', () => {
  const result = plan([runnerSession({ name: 'new-vm', endpoint: 'ep-1' })], [])
  assert.deepEqual(result.orphanNames, ['new-vm'])
  assert.equal(result.upserts.length, 1)
  assert.equal(result.upserts[0]!.endpoint, 'ep-1')
  assert.equal(result.upserts[0]!.expectedAlive, false)
})

test('活会话有 running 任务但 DB 无行 → insert 且 expectedAlive=true（任务兜底认领）', () => {
  const result = plan(
    [runnerSession({ name: 'tasked-vm' })],
    [],
    [task({ id: 't-1', sessionName: 'tasked-vm' })],
  )
  assert.deepEqual(result.orphanNames, [])
  assert.equal(result.upserts[0]!.expectedAlive, true)
})

test('死会话 & expectedAlive=false & 表里记着 idle/busy → 补记 stopped；已 unreachable/stopped 不动', () => {
  const result = plan(
    [],
    [
      dbRow({ name: 'gone-idle', expectedAlive: false, status: 'idle' }),
      dbRow({ name: 'gone-unreachable', expectedAlive: false, status: 'unreachable' }),
      dbRow({ name: 'gone-stopped', expectedAlive: false, status: 'stopped' }),
    ],
  )
  assert.deepEqual(result.markStopped, ['gone-idle'])
  assert.deepEqual(result.markGone, [])
  assert.deepEqual(result.tasksToOrphan, [])
})

test('名称映射丢失（Runner 名称为 "?"）：按 endpoint 报孤儿，不落库', () => {
  const result = plan(
    [runnerSession({ name: '?', endpoint: 'ep-unmapped' })],
    [],
  )
  assert.deepEqual(result.orphanNames, ['? (ep-unmapped)'])
  assert.deepEqual(result.upserts, [])
  // 视图同样检出（横幅依赖它）
  const view = buildSessionViews([runnerSession({ name: '?', endpoint: 'ep-unmapped' })], [], [])
  assert.equal(view[0]!.isOrphan, true)
  assert.equal(view[0]!.name, '? (ep-unmapped)')
  assert.equal(view[0]!.runnerNameKnown, false)
})

test('名称映射丢失但 endpoint 与 DB 行匹配 → 复用该行（endpoint 兜底 join）', () => {
  const view = buildSessionViews(
    [runnerSession({ name: '?', endpoint: 'ep-known' })],
    [dbRow({ name: 'mapped-vm', endpoint: 'ep-known', expectedAlive: true })],
    [],
  )
  assert.equal(view.length, 1)
  assert.equal(view[0]!.name, 'mapped-vm')
  assert.equal(view[0]!.runnerNameKnown, false)
  assert.equal(view[0]!.expectedAlive, true)
  assert.equal(view[0]!.isOrphan, false)
})

test('视图：任务指向的会话两处皆无（dry_run 不建 VM）→ 合成行可见且不算孤儿', () => {
  const view = buildSessionViews(
    [],
    [],
    [task({ id: 't-dry', sessionName: 'p4-dry', status: 'running' })],
  )
  assert.equal(view.length, 1)
  assert.equal(view[0]!.name, 'p4-dry')
  assert.equal(view[0]!.hasRunningTask, true)
  assert.equal(view[0]!.runningTask?.id, 't-dry')
  assert.equal(view[0]!.isOrphan, false)
  assert.equal(view[0]!.inDb, false)
})

test('视图：孤儿置顶排序 + runner 实况优先于 DB 字段', () => {
  const view = buildSessionViews(
    [
      runnerSession({ name: 'z-orphan', variant: 'A100' }),
      runnerSession({ name: 'a-alive', variant: 'T4' }),
    ],
    [dbRow({ name: 'z-orphan', expectedAlive: false, variant: 'L4' }),
     dbRow({ name: 'a-alive', expectedAlive: true })],
    [],
  )
  assert.equal(view[0]!.name, 'z-orphan') // 孤儿置顶
  assert.equal(view[0]!.variant, 'A100') // 实况覆盖 DB 旧值
  assert.equal(view[1]!.name, 'a-alive')
  assert.equal(view[1]!.isOrphan, false)
})

test('视图：DB 独有行（stopped 历史）保留展示、 reachable=false', () => {
  const view = buildSessionViews(
    [],
    [dbRow({ name: 'history', status: 'stopped', stoppedAt: NOW })],
    [],
  )
  assert.equal(view.length, 1)
  assert.equal(view[0]!.reachable, false)
  assert.equal(view[0]!.dbStatus, 'stopped')
  assert.equal(view[0]!.isOrphan, false)
})
