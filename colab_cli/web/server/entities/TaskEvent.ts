import { EntitySchema } from 'typeorm'

// doc/03 §3 TaskEvent：只存里程碑（start/ok/warn/error），不存日志行。
// 禁止规则在 services/taskEvents.ts 的 Repository 层断言强制执行：
// 拒绝 status='progress' 与 phase IN ('metrics.sample','log.line')。
export type TaskEventStatus = 'start' | 'ok' | 'warn' | 'error'

export interface TaskEventRow {
  id: string
  taskId: string
  seq: number
  /** UTC，毫秒精度 */
  ts: Date
  phase: string
  status: TaskEventStatus
  message: string
  data: Record<string, unknown> | null
}

export const TaskEventSchema = new EntitySchema<TaskEventRow>({
  name: 'TaskEvent',
  tableName: 'task_events',
  columns: {
    id: { type: 'bigint', primary: true, generated: 'increment' },
    taskId: { type: 'char', length: 24 },
    seq: { type: 'int' },
    ts: { type: 'datetime', precision: 3 },
    phase: { type: 'varchar', length: 32 },
    status: { type: 'enum', enum: ['start', 'ok', 'warn', 'error'] },
    message: { type: 'text' },
    data: { type: 'json', nullable: true },
  },
  indices: [
    // 唯一索引兼作 SSE 重放去重
    { name: 'UQ_task_events_task_seq', columns: ['taskId', 'seq'], unique: true },
  ],
})
