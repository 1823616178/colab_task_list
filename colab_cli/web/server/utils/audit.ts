import { getRequestIP } from 'h3'
import type { H3Event } from 'h3'
import { getDataSource } from './db'

// 操作审计写入（doc/05 P5）。铁律：审计失败绝不阻断业务操作 —— 只 console.warn。
export interface AuditInput {
  /** 动作名，点分层级：如 auth.login.success / task.cancel / output.cleanup */
  action: string
  /** 操作对象（task_id / workflow slug / 目标用户名…），可空 */
  target?: string | null
  /** 动作上下文（json 列）。绝不写密码、token 等敏感字段 */
  detail?: Record<string, unknown> | null
  /**
   * 显式操作者。缺省取 event.context.auth（JWT 中间件注入）；
   * 登录失败等无 auth 场景由调用方显式给 { userId: null, username: <尝试的账号> }；
   * 系统动作（产物清理）给 { userId: null, username: 'system' } 且 event 传 null。
   */
  user?: { userId: number | null, username: string }
}

export async function writeAudit(event: H3Event | null, input: AuditInput): Promise<void> {
  try {
    const auth = event?.context.auth
    const user = input.user ?? (auth ? { userId: auth.userId, username: auth.username } : { userId: null, username: 'system' })
    const ip = event ? (getRequestIP(event, { xForwardedFor: true }) ?? '') : ''
    // 裸 SQL 插入（同 taskEvents.ts 的 model_cache upsert 先例）：
    // createdAt 走列默认值 CURRENT_TIMESTAMP(6)，规避 TypeORM json 列 insert 的类型怪癖
    await (await getDataSource()).query(
      `INSERT INTO \`audit_log\` (\`userId\`, \`username\`, \`action\`, \`target\`, \`detail\`, \`ip\`)
       VALUES (?, ?, ?, ?, ?, ?)`,
      [
        user.userId,
        // 列长 64：超长输入（如登录尝试的异常用户名）截断，避免审计写入失败
        user.username.slice(0, 64),
        input.action.slice(0, 64),
        input.target?.slice(0, 255) ?? null,
        input.detail ? JSON.stringify(input.detail) : null,
        ip.slice(0, 64),
      ],
    )
  } catch (error) {
    console.warn(`[audit] 写入失败（${input.action} ${input.target ?? ''}）:`, error)
  }
}
