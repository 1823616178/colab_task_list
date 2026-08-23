// task_events 持久化 + tasks 行回写（doc/02 §1.1、doc/03 §3/§4）。
//
// Repository 层断言（doc/05 P3）：insertMilestoneEvent 拒绝
// status='progress' 与 phase IN ('metrics.sample','log.line') ——
// 这两类只走 SSE/落 run.log，永不入库；别只靠调用方自觉。
import { In } from 'typeorm'
import type { QueryDeepPartialEntity } from 'typeorm'
import type { CancelMode, Task, TransportMode } from '../entities/Task'
import { TaskSchema } from '../entities/Task'
import type { TaskEventRow } from '../entities/TaskEvent'
import { TaskEventSchema } from '../entities/TaskEvent'
import { getDataSource } from '../utils/db'

// enum 列白名单：Runner 传来的是普通字符串，写库前收窄，
// 非法值记 null 而不是硬塞进 enum 列（MySQL 严格模式下会直接报错）。
const TRANSPORT_MODES: readonly TransportMode[] = ['native', 'cli', 'dry_run']
const CANCEL_MODES: readonly CancelMode[] = ['interrupt', 'destroy']

/** Runner SSE 事件的形状（契约见 doc/02 §1.1 / web/types/events.d.ts） */
export interface RunnerTaskEvent {
  v: number
  task_id: string
  seq: number
  ts: string
  phase: string
  status: 'start' | 'progress' | 'ok' | 'warn' | 'error'
  message: string
  data: Record<string, unknown>
}

const SSE_ONLY_PHASES = new Set(['metrics.sample', 'log.line'])
const TERMINAL_PHASES = new Set(['task.done', 'task.failed', 'task.canceled'])

/** 入库规则（doc/02 §1.1）：只存 start/ok/warn/error 且非 SSE-only phase。 */
export function isStorableEvent(event: RunnerTaskEvent): boolean {
  return event.status !== 'progress' && !SSE_ONLY_PHASES.has(event.phase)
}

/** Repository 层断言：违反入库规则直接抛错（兜底，调用方应先 isStorableEvent 过滤）。 */
export function assertStorableEvent(event: RunnerTaskEvent): void {
  if (!isStorableEvent(event)) {
    throw new Error(
      `task_events 只存里程碑事件，拒绝入库: status=${event.status} phase=${event.phase}`,
    )
  }
}

export function isTerminalEvent(event: RunnerTaskEvent): boolean {
  return TERMINAL_PHASES.has(event.phase)
}

/**
 * 入库一个里程碑事件 + 回写 tasks 行（lastSeq/currentPhase/startedAt）。
 * (taskId, seq) 唯一索引去重（INSERT IGNORE）：重放/补拉重复到达时静默跳过，
 * 返回是否为新插入。调用方须先过 isStorableEvent。
 */
export async function persistMilestoneEvent(event: RunnerTaskEvent): Promise<boolean> {
  assertStorableEvent(event)
  const ds = await getDataSource()
  const result = await ds
    .getRepository(TaskEventSchema)
    .createQueryBuilder()
    .insert()
    .values({
      taskId: event.task_id,
      seq: event.seq,
      ts: new Date(event.ts),
      phase: event.phase,
      status: event.status as 'start' | 'ok' | 'warn' | 'error',
      message: event.message ?? '',
      // json 列：QueryDeepPartialEntity 会把 Record 的索引签名递归展开成
      // 「每个值都可以是子查询」，与普通对象不兼容，只能在这里断言一次。
      data: (event.data ?? null) as QueryDeepPartialEntity<TaskEventRow>['data'],
    })
    .orIgnore()
    .execute()
  // mysql2 OkPacket：INSERT IGNORE 插入成功 affectedRows=1，撞唯一索引跳过为 0
  const inserted = result.raw?.affectedRows === 1

  const taskUpdates: QueryDeepPartialEntity<Task> = {}
  if (event.status === 'start') taskUpdates.currentPhase = event.phase
  if (event.phase === 'task.start') {
    taskUpdates.status = 'running'
    taskUpdates.startedAt = new Date(event.ts)
  }
  if (Object.keys(taskUpdates).length > 0) {
    await ds.getRepository(TaskSchema).update({ id: event.task_id }, taskUpdates)
  }
  // lastSeq 只增不减（补拉/重连可能乱序到达）
  await ds.query(
    'UPDATE `tasks` SET `lastSeq` = GREATEST(`lastSeq`, ?) WHERE `id` = ?',
    [event.seq, event.task_id],
  )
  return inserted
}

/**
 * 终态回写 tasks 行（doc/03 §4 状态机 + doc/05 P3）：
 * status / finishedAt / durationMs / transportMode / outputZipPath / sha256 等。
 * failed/canceled 且 session_kept → recoverable（VM 保留，远端可能有成品）。
 */
export async function finalizeTaskFromTerminalEvent(event: RunnerTaskEvent): Promise<void> {
  const ds = await getDataSource()
  const data = event.data ?? {}
  const sessionKept = Boolean(data.session_kept)
  const updates: QueryDeepPartialEntity<Task> = {
    finishedAt: new Date(event.ts),
    sessionKept,
    currentPhase: event.phase,
  }
  if (event.phase === 'task.done') {
    updates.status = 'succeeded'
    updates.outputZipPath = typeof data.output_zip === 'string' ? data.output_zip : null
    updates.outputSizeBytes = typeof data.size_bytes === 'number' ? String(data.size_bytes) : null
    updates.outputSha256 = typeof data.sha256 === 'string' ? data.sha256 : null
    updates.transportMode = TRANSPORT_MODES.includes(data.transport as TransportMode)
      ? data.transport as TransportMode
      : null
    updates.durationMs = typeof data.duration_ms === 'number' ? String(data.duration_ms) : null
  } else if (event.phase === 'task.failed') {
    updates.status = sessionKept ? 'recoverable' : 'failed'
    updates.errorCode = typeof data.error_type === 'string' ? data.error_type : null
    updates.errorMessage = typeof data.message === 'string' ? data.message : null
  } else if (event.phase === 'task.canceled') {
    updates.status = sessionKept ? 'recoverable' : 'canceled'
    updates.cancelMode = CANCEL_MODES.includes(data.mode as CancelMode)
      ? data.mode as CancelMode
      : null
  }
  await ds.getRepository(TaskSchema).update({ id: event.task_id }, updates)
}

/**
 * 修复“终态事件已入 task_events/lastSeq，但 tasks.status 尚未终态”的崩溃窗口。
 * 仅接受 seq === tasks.lastSeq 的终态，避免把 redownload 前的旧终态重新套用。
 */
export async function finalizeTaskFromStoredTerminalEvent(
  taskId: string,
  lastSeq: number,
): Promise<boolean> {
  const ds = await getDataSource()
  const row = await ds.getRepository(TaskEventSchema).findOne({
    where: {
      taskId,
      seq: lastSeq,
      phase: In([...TERMINAL_PHASES]),
    },
  })
  if (!row) return false
  await finalizeTaskFromTerminalEvent({
    v: 1,
    task_id: taskId,
    seq: row.seq,
    ts: row.ts.toISOString(),
    phase: row.phase,
    status: row.status,
    message: row.message,
    data: row.data ?? {},
  })
  return true
}

/**
 * ModelCache 更新（doc/03 §3）：由 model.prepare 事件的 data 驱动。
 * progress 事件不入 task_events，但 data 里带 repo/filename/source —— 这里消费。
 * 事件没有 comfyDir/cachePath 字段：取 filename 首段 / filename 本身。
 * 尽力而为：失败只告警，不阻断事件流。
 */
export async function upsertModelCacheFromEvent(event: RunnerTaskEvent): Promise<void> {
  if (event.phase !== 'model.prepare') return
  const data = event.data ?? {}
  if (typeof data.repo !== 'string' || typeof data.filename !== 'string') return
  const source = typeof data.source === 'string' ? data.source : ''
  if (!['drive_cache', 'huggingface', 'vm_local'].includes(source)) return
  try {
    const ds = await getDataSource()
    await ds.query(
      `INSERT INTO \`model_cache\`
         (\`repo\`, \`filename\`, \`comfyDir\`, \`cachePath\`, \`sizeBytes\`, \`present\`, \`lastSource\`, \`lastVerifiedAt\`)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
       ON DUPLICATE KEY UPDATE
         \`sizeBytes\` = VALUES(\`sizeBytes\`),
         \`present\` = VALUES(\`present\`),
         \`lastSource\` = VALUES(\`lastSource\`),
         \`lastVerifiedAt\` = VALUES(\`lastVerifiedAt\`)`,
      [
        data.repo,
        data.filename,
        data.filename.split('/')[0],
        data.filename,
        typeof data.size_bytes === 'number' ? String(data.size_bytes) : '0',
        // present：上次任务是否命中缓存（drive_cache 即命中；vm_local 视为已在机上）
        source === 'huggingface' ? 0 : 1,
        source,
        new Date(event.ts),
      ],
    )
  } catch (error) {
    console.warn(`[model_cache] 更新失败（${data.repo}/${data.filename}）:`, error)
  }
}

export async function findTask(taskId: string): Promise<Task | null> {
  const ds = await getDataSource()
  return await ds.getRepository(TaskSchema).findOne({ where: { id: taskId } })
}
