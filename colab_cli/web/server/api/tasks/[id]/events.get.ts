// GET /api/tasks/:id/events?from_seq=N（doc/02 §3.2/§3.3）：浏览器侧 SSE。
//
// 纪律：本端点**只是订阅者**，不驱动 Nitro→Runner 订阅的生命周期 ——
// 后者由 taskSubscription 按任务生命周期持有。这里做的是：
//   1. 从 task_events 表回放 seq > from_seq 的已入库里程碑（断线续订）
//   2. 注册浏览器订阅者，实时透传订阅管理器广播的原始帧
//      （含 progress/metrics.sample/log.line —— 它们不入库，只在实时流里）
// 任务已终态：回放后立即关闭（与 Runner 侧语义一致）。
import { getQuery, getRouterParam, setResponseHeaders } from 'h3'
import { TaskEventSchema } from '../../../entities/TaskEvent'
import type { TaskEventRow } from '../../../entities/TaskEvent'
import { TERMINAL_TASK_STATUSES } from '../../../entities/Task'
import { findTask } from '../../../services/taskEvents'
import { ensureTaskSubscription, subscribeBrowser } from '../../../services/taskSubscription'
import { getDataSource } from '../../../utils/db'
import { sendApiError } from '../../../utils/errors'

const TERMINAL_PHASE_PATTERN = /"phase":"task\.(done|failed|canceled)"/

function milestoneFrame(row: TaskEventRow): string {
  const data = JSON.stringify({
    v: 1,
    task_id: row.taskId,
    seq: row.seq,
    ts: new Date(row.ts).toISOString(),
    phase: row.phase,
    status: row.status,
    message: row.message,
    data: row.data ?? {},
  })
  return `id: ${row.seq}\nevent: task\ndata: ${data}\n\n`
}

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')!
  const fromSeq = Math.max(-1, Number(getQuery(event).from_seq ?? -1) || 0)
  const task = await findTask(id)
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)

  setResponseHeaders(event, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  })

  const encoder = new TextEncoder()
  const taskId = id
  let onCancel: (() => void) | null = null
  return new ReadableStream<Uint8Array>({
    async start(controller) {
      let closed = false
      let unsubscribe: (() => void) | null = null
      // 先挂订阅再回放：两者之间的实时事件进缓冲，回放结束后按 seq 去重补发
      const buffer: string[] = []
      let replaying = true

      const close = () => {
        if (closed) return
        closed = true
        unsubscribe?.()
        try {
          controller.close()
        } catch { /* 已关闭 */ }
      }
      const push = (frame: string) => {
        if (closed) return
        try {
          controller.enqueue(encoder.encode(frame))
        } catch {
          close()
          return
        }
        if (TERMINAL_PHASE_PATTERN.test(frame)) close() // 终态后流关闭（doc/02 §2.5）
      }
      unsubscribe = subscribeBrowser(taskId, (frame) => {
        if (replaying) {
          buffer.push(frame)
        } else {
          push(frame)
        }
      })

      const isTerminal = TERMINAL_TASK_STATUSES.includes(task.status)
      if (!isTerminal) {
        // 兜底：正常路径下订阅已由受理/启动补拉建立；这里覆盖罕见漏网场景
        ensureTaskSubscription(taskId)
      }

      let lastSeq = fromSeq
      try {
        const ds = await getDataSource()
        const rows = await ds.getRepository(TaskEventSchema).find({
          where: { taskId },
          order: { seq: 'ASC' },
        })
        for (const row of rows) {
          if (row.seq <= fromSeq) continue
          lastSeq = Math.max(lastSeq, row.seq)
          push(milestoneFrame(row))
        }
      } catch (error) {
        console.error(`[task-events] ${taskId} 回放失败:`, error)
      }
      replaying = false
      // 补发缓冲中 seq > 回放水位的帧（seq 从帧文本取，避免整段 JSON.parse）
      for (const frame of buffer) {
        const match = frame.match(/"seq":(\d+)/)
        if (match && Number(match[1]) <= lastSeq) continue
        push(frame)
      }
      buffer.length = 0

      // 回放完成时任务已终态（含回放期间刚好收尾）→ 关闭
      if (closed) return
      const fresh = await findTask(taskId).catch(() => null)
      if (!fresh || TERMINAL_TASK_STATUSES.includes(fresh.status)) close()

      onCancel = close
      event.node.req.on('close', close) // 浏览器断开：只摘订阅，不动订阅管理器
    },
    cancel() {
      onCancel?.() // ReadableStream 取消（连接被对端中止）
    },
  })
})
