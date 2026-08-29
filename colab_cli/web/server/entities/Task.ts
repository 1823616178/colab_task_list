import { EntitySchema } from 'typeorm'

// doc/03 §3 Task + §4 状态机。id 沿用 create_task_id() 格式（char(24)，非自增）。
// 「同一 session 只允许一个非终态任务」用 activeSessionKey 生成列 + 唯一索引实现
// （MySQL 无部分唯一约束）：非终态（queued/running）时取 sessionName，否则 NULL。
export type TaskStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'recoverable'
  | 'canceled'
  | 'orphaned'

export type CancelMode = 'interrupt' | 'destroy'
// dry_run 是 Runner 的演练通道（不烧计算单元），仅 dry_run 任务出现
export type TransportMode = 'native' | 'cli' | 'dry_run'
export type FtpUploadStatus = 'pending' | 'uploading' | 'uploaded' | 'failed'

export interface Task {
  /** YYYYMMDD-HHMMSS-<uuid4 前 8 位>，与 Runner 生成的 task_id 一致 */
  id: string
  workflowConfigId: number
  /** 冗余快照，配置改名后历史仍可读 */
  workflowName: string
  /** 弱引用，不设 FK（会话会被删） */
  sessionName: string
  acceleratorKind: 'gpu' | 'tpu'
  acceleratorSpec: string
  mountDrive: boolean
  reuse: boolean
  keep: boolean
  timeout: number
  status: TaskStatus
  /** 最后一个 start 事件的 phase */
  currentPhase: string | null
  queuedAt: Date
  startedAt: Date | null
  finishedAt: Date | null
  /** bigint → mysql2 返回 string */
  durationMs: string | null
  exitCode: number | null
  /** 提交瞬间的 api.json 快照（doc/02 §2.3.1） */
  apiJsonSnapshotPath: string
  workflowSnapshotPath: string
  /** colab_output/<id>/events.jsonl */
  eventsPath: string
  /** 已入库的最大 seq，重连对账用 */
  lastSeq: number
  cancelMode: CancelMode | null
  outputZipPath: string | null
  outputSizeBytes: string | null
  outputSha256: string | null
  /** 本地产物异步镜像到 FTP；不影响任务 succeeded 终态。 */
  ftpStatus: FtpUploadStatus | null
  ftpRemotePath: string | null
  ftpUploadedAt: Date | null
  ftpError: string | null
  ftpAttempts: number
  /** colab_output/<id>/run.log */
  logPath: string | null
  /** 失败保护是否触发 */
  sessionKept: boolean
  transportMode: TransportMode | null
  errorCode: string | null
  errorMessage: string | null
  createdByUserId: number | null
  /** 生成列：非终态时 = sessionName，唯一索引兜底 session 互斥；只读映射 */
  activeSessionKey: string | null
}

export const TERMINAL_TASK_STATUSES: TaskStatus[] = [
  'succeeded',
  'failed',
  'recoverable',
  'canceled',
  'orphaned',
]

export const TaskSchema = new EntitySchema<Task>({
  name: 'Task',
  tableName: 'tasks',
  columns: {
    id: { type: 'char', length: 24, primary: true },
    workflowConfigId: { type: 'int' },
    workflowName: { type: 'varchar', length: 255 },
    sessionName: { type: 'varchar', length: 128 },
    acceleratorKind: { type: 'enum', enum: ['gpu', 'tpu'] },
    acceleratorSpec: { type: 'varchar', length: 16 },
    mountDrive: { type: 'boolean', default: true },
    reuse: { type: 'boolean', default: false },
    keep: { type: 'boolean', default: false },
    timeout: { type: 'double', default: 86400 },
    status: {
      type: 'enum',
      enum: ['queued', 'running', 'succeeded', 'failed', 'recoverable', 'canceled', 'orphaned'],
      default: 'queued',
    },
    currentPhase: { type: 'varchar', length: 32, nullable: true },
    queuedAt: { type: 'datetime', createDate: true },
    startedAt: { type: 'datetime', nullable: true },
    finishedAt: { type: 'datetime', nullable: true },
    durationMs: { type: 'bigint', nullable: true },
    exitCode: { type: 'int', nullable: true },
    apiJsonSnapshotPath: { type: 'varchar', length: 512 },
    workflowSnapshotPath: { type: 'varchar', length: 512 },
    eventsPath: { type: 'varchar', length: 512 },
    lastSeq: { type: 'int', default: -1 },
    cancelMode: { type: 'enum', enum: ['interrupt', 'destroy'], nullable: true },
    outputZipPath: { type: 'varchar', length: 512, nullable: true },
    outputSizeBytes: { type: 'bigint', nullable: true },
    outputSha256: { type: 'char', length: 64, nullable: true },
    ftpStatus: {
      type: 'enum',
      enum: ['pending', 'uploading', 'uploaded', 'failed'],
      nullable: true,
    },
    ftpRemotePath: { type: 'varchar', length: 512, nullable: true },
    ftpUploadedAt: { type: 'datetime', nullable: true },
    ftpError: { type: 'text', nullable: true },
    ftpAttempts: { type: 'int', default: 0 },
    logPath: { type: 'varchar', length: 512, nullable: true },
    sessionKept: { type: 'boolean', default: false },
    transportMode: { type: 'enum', enum: ['native', 'cli', 'dry_run'], nullable: true },
    errorCode: { type: 'varchar', length: 255, nullable: true },
    errorMessage: { type: 'text', nullable: true },
    createdByUserId: { type: 'int', nullable: true },
    activeSessionKey: {
      type: 'varchar',
      length: 160,
      nullable: true,
      generatedType: 'STORED',
      asExpression: "IF(`status` IN ('queued','running'), `sessionName`, NULL)",
    },
  },
  indices: [
    { name: 'IDX_tasks_status_queued', columns: ['status', 'queuedAt'] },
    { name: 'IDX_tasks_session_status', columns: ['sessionName', 'status'] },
    { name: 'IDX_tasks_workflow_config', columns: ['workflowConfigId'] },
    { name: 'IDX_tasks_ftp_status', columns: ['ftpStatus', 'finishedAt'] },
  ],
})
