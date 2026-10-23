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
import { findTask } from '../../../services/taskEvents'
import { ensureTaskSubscription, subscribeBrowser } from '../../../services/taskSubscription'
import { getDataSource } from '../../../utils/db'
import { sendApiError } from '../../../utils/errors'
import { createTaskBrowserStream } from '../../../services/taskBrowserStream'

function milestoneEvent(row: TaskEventRow) {
  return {
    v: 1,
    task_id: row.taskId,
    seq: row.seq,
    ts: new Date(row.ts).toISOString(),
    phase: row.phase,
    status: row.status,
    message: row.message,
    data: row.data ?? {},
  }
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

  const { stream, close } = createTaskBrowserStream({
    taskId: id,
    fromSeq,
    subscribe: push => subscribeBrowser(id, push),
    loadHistory: async () => {
      const ds = await getDataSource()
      const rows = await ds.getRepository(TaskEventSchema).find({
        where: { taskId: id },
        order: { seq: 'ASC' },
      })
      return rows.map(milestoneEvent)
    },
    loadTask: async () => {
      const fresh = await findTask(id)
      return fresh ? { status: fresh.status, lastSeq: fresh.lastSeq } : null
    },
    ensureSubscription: () => ensureTaskSubscription(id),
  })
  event.node.res.once('close', close)
  return stream
})
