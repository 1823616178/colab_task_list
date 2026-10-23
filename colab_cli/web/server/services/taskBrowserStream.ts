interface StreamEvent {
  task_id: string
  seq: number
  phase: string
}

interface StreamTask {
  status: string
  lastSeq: number
}

export interface TaskBrowserStreamDeps {
  taskId: string
  fromSeq: number
  subscribe: (push: (frame: string) => void) => () => void
  loadHistory: () => Promise<StreamEvent[]>
  loadTask: () => Promise<StreamTask | null>
  ensureSubscription: () => void
}

const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'recoverable', 'canceled', 'orphaned'])
const TERMINAL_PHASES = new Set(['task.done', 'task.failed', 'task.canceled'])

/** 历史终态是时间线记录；只有实时终态或最新任务快照才会结束本次监控。 */
export function createTaskBrowserStream(deps: TaskBrowserStreamDeps): {
  stream: ReadableStream<Uint8Array>
  close: () => void
} {
  let close = () => {}
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const encoder = new TextEncoder()
      let closed = false
      let replaying = true
      let lastSeq = deps.fromSeq
      const buffer: string[] = []
      close = () => {
        if (closed) return
        closed = true
        unsubscribe?.()
        try { controller.close() } catch { /* 已取消 */ }
      }
      const push = (frame: string) => {
        if (!closed) controller.enqueue(encoder.encode(frame))
      }
      const live = (frame: string) => {
        if (closed) return
        const data = frame.split('\n').find(line => line.startsWith('data:'))
        if (!data) { push(frame); return }
        const event = JSON.parse(data.slice(5)) as StreamEvent
        if (event.task_id !== deps.taskId || !Number.isSafeInteger(event.seq) || event.seq <= lastSeq) return
        lastSeq = event.seq
        push(frame)
        if (TERMINAL_PHASES.has(event.phase)) close()
      }
      const unsubscribe = deps.subscribe(frame => replaying ? buffer.push(frame) : live(frame))
      try {
        const rows = await deps.loadHistory()
        if (closed) return
        for (const event of rows) {
          if (event.seq <= lastSeq) continue
          lastSeq = event.seq
          push(`id: ${event.seq}\nevent: replay\ndata: ${JSON.stringify(event)}\n\n`)
        }
        const task = await deps.loadTask()
        if (closed) return
        // 查询期间的实时事件仍在缓冲；先补齐再判断是否已终态。
        replaying = false
        for (const frame of buffer) live(frame)
        buffer.length = 0
        if (closed) return
        if (!task) { close(); return }
        push(`event: snapshot\ndata: ${JSON.stringify(task)}\n\n`)
        if (TERMINAL_STATUSES.has(task.status)) close()
        else deps.ensureSubscription()
      } catch (error) {
        unsubscribe?.()
        if (!closed) {
          closed = true
          controller.error(error) // 回放失败必须重连，不能跳过历史直接推进水位
        }
      }
    },
    cancel() { close() },
  })
  return { stream, close: () => close() }
}
