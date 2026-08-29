import { EntitySchema } from 'typeorm'

// doc/05 P5 操作审计日志（migration 008）。
// userId 故意不设 FK：审计记录的生命周期独立于用户行（测试账号物理删除后审计仍留存）。
export interface AuditLog {
  /** bigint → mysql2 返回 string（与 TaskEvent.id 同口径） */
  id: string
  /** 操作者；系统动作（如产物清理）为 null */
  userId: number | null
  /** 冗余快照，系统动作为 'system' */
  username: string
  /** 如 auth.login.success / task.cancel / output.cleanup */
  action: string
  /** 操作对象（task_id / workflow slug / 用户名…） */
  target: string | null
  /** 动作上下文（非敏感；绝不写密码/token） */
  detail: Record<string, unknown> | null
  /** 系统动作为空串 */
  ip: string
  createdAt: Date
}

export const AuditLogSchema = new EntitySchema<AuditLog>({
  name: 'AuditLog',
  tableName: 'audit_log',
  columns: {
    id: { type: 'bigint', primary: true, generated: 'increment' },
    userId: { type: 'int', nullable: true },
    username: { type: 'varchar', length: 64 },
    action: { type: 'varchar', length: 64 },
    target: { type: 'varchar', length: 255, nullable: true },
    detail: { type: 'json', nullable: true },
    ip: { type: 'varchar', length: 64, default: '' },
    createdAt: { type: 'datetime', createDate: true },
  },
  indices: [
    { name: 'IDX_audit_log_created', columns: ['createdAt'] },
    { name: 'IDX_audit_log_user_created', columns: ['userId', 'createdAt'] },
  ],
})
