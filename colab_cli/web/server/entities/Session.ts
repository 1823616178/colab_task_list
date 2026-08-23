import { EntitySchema } from 'typeorm'

// doc/03 §3 Session：防孤儿 VM 的核心表。P3 只建表与基本读写，
// 启动/定时对账在 P4 落地。
export type SessionStatus = 'idle' | 'busy' | 'unreachable' | 'stopped'
export type SessionAccelerator = 'gpu' | 'tpu' | 'cpu'

export interface Session {
  id: number
  /** 如 comfyui-h3 */
  name: string
  accelerator: SessionAccelerator | null
  /** A100 / L4 / TPU v6e-1 */
  variant: string | null
  /** 名称映射丢失时按 endpoint 停止 */
  endpoint: string | null
  status: SessionStatus
  /** 我们认为它该活着 */
  expectedAlive: boolean
  createdAt: Date
  lastSeenAt: Date | null
  stoppedAt: Date | null
  /** 上次对账时间 */
  reconciledAt: Date | null
}

export const SessionSchema = new EntitySchema<Session>({
  name: 'Session',
  tableName: 'sessions',
  columns: {
    id: { type: 'int', primary: true, generated: 'increment' },
    name: { type: 'varchar', length: 128, unique: true },
    accelerator: { type: 'enum', enum: ['gpu', 'tpu', 'cpu'], nullable: true },
    variant: { type: 'varchar', length: 32, nullable: true },
    endpoint: { type: 'varchar', length: 255, nullable: true },
    status: { type: 'enum', enum: ['idle', 'busy', 'unreachable', 'stopped'] },
    expectedAlive: { type: 'boolean', default: false },
    createdAt: { type: 'datetime', createDate: true },
    lastSeenAt: { type: 'datetime', nullable: true },
    stoppedAt: { type: 'datetime', nullable: true },
    reconciledAt: { type: 'datetime', nullable: true },
  },
})
