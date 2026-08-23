// task 事件订阅管理器（doc/02 §3.3 的核心纪律）：
//
// **SSE 订阅由任务生命周期驱动，不由浏览器连接驱动。**
// 每个非终态任务一条 Nitro→Runner 的 SSE 长连接，从任务受理持续到终态：
//   ① 事件原样转发给浏览器订阅者（含 progress/metrics.sample/log.line）
//   ② start/ok/warn/error 写入 task_events（经 taskEvents 的入库断言）
//   ③ 终态事件回写 tasks 行（status/output/sha256/durationMs/transportMode…）
// 浏览器断开不影响这条订阅；Nitro↔Runner 断线按指数退避重连，
// 用 from_seq=<已处理的最大 seq> 续订补漏（Runner 侧 events.jsonl 落盘回放）。
// Nitro 重启后的启动补拉见 plugins/03.task-recovery.ts。
// P4 会话对账（services/sessionReconcile.ts）发现 VM 已消失时经
// stopTaskSubscription() 结束订阅 —— 同样是生命周期驱动（任务已不可能再推进）。
import { TaskSchema } from '../entities/Task'
import { getDataSource } from '../utils/db'
import { runnerStream } from './runnerClient'
import {
  finalizeTaskFromTerminalEvent,
  isStorableEvent,
  isTerminalEvent,
  persistMilestoneEvent,
  upsertModelCacheFromEvent,
} from './taskEvents'
import type { RunnerTaskEvent } from './taskEvents'
import { scheduleTaskFtpUpload } from './ftpUpload'

interface Subscription {
  taskId: string
  /** 已处理（广播+入库链路）的最大 seq；重连用 from_seq */
  lastSeq: number
  terminal: boolean
  controller: AbortController | null
  /** 事件处理串行链：保序，终态回写一定在事件入库之后 */
  chain: Promise<void>
}

// globalThis 缓存：dev 热更新时订阅表不丢（与 db.ts 同套路）
declare global {
  var __colabTaskSubs: Map<string, Subscription> | undefined
  var __colabBrowserSubs: Map<string, Set<(frame: string) => void>> | undefined
}

function subscriptions(): Map<string, Subscription> {
  globalThis.__colabTaskSubs ??= new Map()
  return globalThis.__colabTaskSubs
}

function browserSubscribers(): Map<string, Set<(frame: string) => void>> {
  globalThis.__colabBrowserSubs ??= new Map()
  return globalThis.__colabBrowserSubs
}

const RECONNECT_INITIAL_MS = 1_000
const RECONNECT_MAX_MS = 30_000

/** 该任务是否已有活着的 Nitro→Runner 订阅 */
export function hasTaskSubscription(taskId: string): boolean {
  const sub = subscriptions().get(taskId)
  return !!sub && !sub.terminal
}

/**
 * 主动结束订阅（P4 会话对账用）：VM 已消失、任务不可能再推进时调用。
 * 置终态标志 + abort 当前连接；重连循环随之退出，finally 摘除订阅表。
 * 幂等：无订阅或已终态时静默返回。
 */
export function stopTaskSubscription(taskId: string): void {
  const sub = subscriptions().get(taskId)
  if (!sub || sub.terminal) return
  sub.terminal = true
  sub.controller?.abort()
}

/**
 * 确保任务有到 Runner 的 SSE 订阅（幂等）。已在订阅中直接返回。
 * fromSeq 缺省读 tasks.lastSeq（启动补拉场景由插件显式传入同值）。
 */
export function ensureTaskSubscription(taskId: string, fromSeq?: number): void {
  if (hasTaskSubscription(taskId)) return
  const sub: Subscription = {
    taskId,
    lastSeq: fromSeq ?? -1,
    terminal: false,
    controller: null,
    chain: Promise.resolve(),
  }
  subscriptions().set(taskId, sub)
  if (fromSeq === undefined) {
    // 缺省从 DB 取 lastSeq：覆盖 Nitro 宕机窗口（doc/05 P3 启动补拉）
    void getDataSource()
      .then(ds => ds.getRepository(TaskSchema).findOne({ where: { id: taskId } }))
      .then((task) => {
        if (task) sub.lastSeq = Math.max(sub.lastSeq, task.lastSeq)
      })
      .catch(error => console.warn(`[task-sub] 读取 ${taskId} lastSeq 失败:`, error))
      .finally(() => void runSubscriptionLoop(sub))
  } else {
    void runSubscriptionLoop(sub)
  }
}

/** 注册浏览器订阅者（原始 SSE frame 文本透传）；返回取消注册函数。 */
export function subscribeBrowser(taskId: string, push: (frame: string) => void): () => void {
  const subs = browserSubscribers()
  let set = subs.get(taskId)
  if (!set) {
    set = new Set()
    subs.set(taskId, set)
  }
  set.add(push)
  return () => {
    set.delete(push)
    if (set.size === 0) subs.delete(taskId)
  }
}

function broadcast(sub: Subscription, frame: string): void {
  const set = browserSubscribers().get(sub.taskId)
  if (!set) return
  for (const push of set) {
    try {
      push(frame)
    } catch {
      // 单个浏览器连接写失败不影响其他订阅者与订阅本身
    }
  }
}

async function runSubscriptionLoop(sub: Subscription): Promise<void> {
  let backoffMs = RECONNECT_INITIAL_MS
  try {
    while (!sub.terminal) {
      sub.controller = new AbortController()
      let sawEvent = false
      try {
        const response = await runnerStream(
          `/tasks/${sub.taskId}/events?from_seq=${sub.lastSeq}`,
          sub.controller.signal,
        )
        if (!response.ok) {
          if (response.status === 404) {
            // Runner 侧连 events.jsonl 都没了（任务目录被清）→ 状态不可知
            console.warn(`[task-sub] ${sub.taskId} 在 Runner 已无记录，标记 orphaned`)
            await markOrphaned(sub.taskId)
            sub.terminal = true
            return
          }
          throw new Error(`Runner SSE HTTP ${response.status}`)
        }
        if (!response.body) throw new Error('Runner SSE 响应无 body')
        await consumeSseStream(response.body, (rawFrame, data) => {
          if (data === null) {
            broadcast(sub, rawFrame) // `: keepalive` 等注释帧原样透传
            return
          }
          sawEvent = true
          handleEventFrame(sub, rawFrame, data)
        })
        // 等入库/回写串行链落定：终态标志在链里才置位，避免流刚关闭就盲目重连一轮
        await sub.chain
      } catch (error) {
        if (sub.terminal) return
        console.warn(`[task-sub] ${sub.taskId} 订阅中断:`, error instanceof Error ? error.message : error)
      }
      if (sub.terminal) return
      // 流被关闭但未见终态（Runner 重启/网络抖动）→ 指数退避重连补拉。
      // 退避只在「这轮真的收到了事件」时复位：Runner 侧 200 但零事件立即关流的
      // 异常情形（如 Runner 终态事件丢失）下避免 1s 热循环空转。
      backoffMs = sawEvent ? RECONNECT_INITIAL_MS : Math.min(backoffMs * 2, RECONNECT_MAX_MS)
      await new Promise(resolve => setTimeout(resolve, backoffMs))
    }
  } finally {
    subscriptions().delete(sub.taskId)
  }
}

function handleEventFrame(sub: Subscription, rawFrame: string, data: string): void {
  let event: RunnerTaskEvent
  try {
    event = JSON.parse(data) as RunnerTaskEvent
  } catch {
    console.warn(`[task-sub] ${sub.taskId} 收到无法解析的事件帧，已忽略`)
    return
  }
  if (typeof event.seq !== 'number' || event.seq <= sub.lastSeq) return // 重放去重
  sub.lastSeq = event.seq
  const terminal = isTerminalEvent(event)
  // 非终态保持低延迟；终态必须等 tasks 行回写完成后广播，避免详情页回查仍是 running。
  if (!terminal) broadcast(sub, rawFrame)

  // 入库/回写串行链：与广播解耦，失败只告警不打断流
  sub.chain = sub.chain.then(async () => {
    try {
      if (isStorableEvent(event)) {
        await persistMilestoneEvent(event)
      }
      await upsertModelCacheFromEvent(event)
      if (terminal) {
        await finalizeTaskFromTerminalEvent(event)
        if (event.phase === 'task.done') {
          try {
            await scheduleTaskFtpUpload(event.task_id)
          } catch (error) {
            // FTP 是成功产物的异步镜像，配置/入队失败绝不能挡住 succeeded 与下载按钮。
            console.warn(
              `[ftp-upload] ${event.task_id} 入队失败:`,
              error instanceof Error ? error.message : error,
            )
          }
        }
        broadcast(sub, rawFrame)
        sub.terminal = true
        sub.controller?.abort() // 终态后主动断开（Runner 也会关流）
      }
    } catch (error) {
      console.error(`[task-sub] ${sub.taskId} 事件 seq=${event.seq} 入库失败:`, error)
    }
  })
}

async function markOrphaned(taskId: string): Promise<void> {
  try {
    const ds = await getDataSource()
    await ds.getRepository(TaskSchema).update(
      { id: taskId },
      { status: 'orphaned', finishedAt: new Date(), errorCode: 'RUNNER_LOST', errorMessage: 'Runner 侧已无该任务记录' },
    )
  } catch (error) {
    console.error(`[task-sub] ${taskId} 标记 orphaned 失败:`, error)
  }
}

/** 逐块解析 SSE 流：完整帧（\n\n 分隔）回调；注释帧 data 为 null。 */
async function consumeSseStream(
  body: ReadableStream<Uint8Array>,
  onFrame: (rawFrame: string, data: string | null) => void,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) return
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        boundary = buffer.indexOf('\n\n')
        let data: string | null = null
        for (const line of block.split('\n')) {
          if (line.startsWith('data:')) {
            data = (data ?? '') + (data === null ? '' : '\n') + line.slice(5).replace(/^ /, '')
          }
        }
        onFrame(block + '\n\n', data)
      }
    }
  } finally {
    reader.releaseLock()
  }
}
