import { defineStore } from 'pinia'
import type { TaskEvent } from '~/types/events'
import { finiteMetric as asNumber, sampleIsStale, workflowProgress } from '~/utils/taskMonitoring'
import type { WorkflowProgress } from '~/utils/taskMonitoring'
import { mergeLogHistory, parseRunLog } from '~/utils/logHistory'

// doc/04 §3.2：taskStream 单任务 SSE——事件、日志、指标（性能关键）。
//
// 实现说明：
// - 不用原生 EventSource：/api/** 走 JWT Bearer 头（server/middleware/auth.ts），
//   EventSource 无法带自定义头。改用 fetch + ReadableStream 手写 SSE 解析，
//   401 时复用 apiFetch 插件的合流刷新（$apiRefresh）后重连。
// - logLines：shallowRef 环形缓冲，上限 2000 行；高频 log.line 先写入非响应式
//   数组，每 150ms 批量刷一次快照，避免整页反复重渲染。
// - metrics：定长环形缓冲，只保留最近 150 个采样点（约 5 分钟）。
// - phases：低频（start/ok/warn/error 入库里程碑），普通 ref 深响应。
// - 断线按 from_seq=<lastSeq> 续订，指数退避 1s → 2s → 4s → 上限 30s。
// - SSE 建连后拉一次 run.log 历史并与建连期间的实时行去重合并；刷新运行中任务
//   也能恢复日志。任务终态后不再重连（log.line 不入事件库，doc/02 §2.5）。

const LOG_CAP = 2000
const METRICS_CAP = 150
const LOG_FLUSH_MS = 150
const BACKOFF_INIT_MS = 1000
const BACKOFF_MAX_MS = 30000

const TERMINAL_PHASES = new Set(['task.done', 'task.failed', 'task.canceled'])

export interface MetricSample {
  ts: string
  gpuUtil: number
  gpuUtilAvg30: number | null
  gpuUtilPeak: number | null
  vramUsedMb: number
  vramTotalMb: number
  ramUsedMb: number | null
  cpuPercent: number | null
}

export interface PhaseNode {
  phase: string
  /** active = 已 start 未完结 */
  status: 'active' | 'ok' | 'warn' | 'error'
  startTs: string
  endTs: string | null
  message: string
  seq: number
}

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed'

export const useTaskStreamStore = defineStore('taskStream', () => {
  const taskId = ref<string | null>(null)
  const connectionState = ref<ConnectionState>('idle')
  /** 已收到终态事件（或打开时已知终态），不再重连 */
  const terminal = ref(false)
  const lastSeq = ref(-1)
  const errorMessage = ref('')
  const progress = shallowRef<WorkflowProgress | null>(null)
  const now = ref(Date.now())
  const openedAt = ref(Date.now())
  const metricsStale = computed(() => !terminal.value
    && sampleIsStale(metrics.value.at(-1)?.ts ?? null, openedAt.value, now.value))

  const logLines = shallowRef<string[]>([])
  const metrics = shallowRef<MetricSample[]>([])
  const phases = ref<PhaseNode[]>([])
  const driveAuthorizationUrl = ref<string | null>(null)

  // ── 非响应式内部状态（高频路径不进响应式系统）─────────────────
  let linesRaw: string[] = []
  /** 末尾一行是 \r 刷新行，下一条 \r 行应就地覆盖它 */
  let rawOverwritable = false
  let flushTimer: ReturnType<typeof setTimeout> | null = null

  let abortController: AbortController | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let backoffMs = BACKOFF_INIT_MS
  let stopped = true
  let logHistoryLoaded = false
  let freshnessTimer: ReturnType<typeof setInterval> | null = null
  let lastFrameAt = 0

  function flushLogs() {
    flushTimer = null
    logLines.value = linesRaw.slice()
  }

  function scheduleFlush() {
    if (flushTimer) return
    flushTimer = setTimeout(flushLogs, LOG_FLUSH_MS)
  }

  /** log.line：progress 类的 \r 刷新行就地覆盖，不追加（doc/04 §2.2） */
  function pushLog(text: string) {
    const segments = text.split('\r')
    const visible = segments[segments.length - 1] ?? ''
    if (segments.length > 1 && rawOverwritable && linesRaw.length > 0) {
      linesRaw[linesRaw.length - 1] = visible
    } else {
      linesRaw.push(visible)
    }
    rawOverwritable = segments.length > 1
    if (linesRaw.length > LOG_CAP) linesRaw.splice(0, linesRaw.length - LOG_CAP)
    scheduleFlush()
  }

  function pushMetric(ev: TaskEvent) {
    const d = ev.data ?? {}
    const sample: MetricSample = {
      ts: ev.ts,
      gpuUtil: asNumber(d.gpu_util) ?? 0,
      gpuUtilAvg30: asNumber(d.gpu_util_avg30),
      gpuUtilPeak: asNumber(d.gpu_util_peak),
      vramUsedMb: asNumber(d.vram_used_mb) ?? 0,
      vramTotalMb: asNumber(d.vram_total_mb) ?? 0,
      ramUsedMb: asNumber(d.ram_used_mb),
      cpuPercent: asNumber(d.cpu_percent),
    }
    const next = metrics.value.length >= METRICS_CAP
      ? metrics.value.slice(metrics.value.length - METRICS_CAP + 1)
      : metrics.value.slice()
    next.push(sample)
    metrics.value = next
  }

  /** start/ok/warn/error 里程碑 → 阶段时间线（doc/04 §2.2） */
  function applyMilestone(ev: TaskEvent) {
    if (ev.status === 'start') {
      // 同 phase 已有未完结节点（如重试重发 start）则忽略
      if (phases.value.some(n => n.phase === ev.phase && n.status === 'active')) return
      phases.value.push({
        phase: ev.phase,
        status: 'active',
        startTs: ev.ts,
        endTs: null,
        message: ev.message,
        seq: ev.seq,
      })
      return
    }
    if (ev.status === 'progress') return
    // ok / warn / error：完结最后一个同 phase 的 active 节点；没有则补一条已完结节点
    const node = [...phases.value].reverse()
      .find(n => n.phase === ev.phase && n.status === 'active')
    if (node) {
      node.status = ev.status
      node.endTs = ev.ts
      if (ev.message) node.message = ev.message
    } else {
      phases.value.push({
        phase: ev.phase,
        status: ev.status,
        startTs: ev.ts,
        endTs: ev.ts,
        message: ev.message,
        seq: ev.seq,
      })
    }
  }

  function handleEvent(ev: TaskEvent, replay = false) {
    if (ev.task_id !== taskId.value || !Number.isSafeInteger(ev.seq) || ev.seq <= lastSeq.value) return
    lastSeq.value = ev.seq
    if (ev.phase === 'drive.mount') {
      const candidate = ev.data?.authorization_url
      if (typeof candidate === 'string') {
        try {
          const parsed = new URL(candidate)
          if (parsed.protocol === 'https:' && parsed.hostname === 'accounts.google.com') {
            driveAuthorizationUrl.value = parsed.toString()
          }
        } catch {
          // Runner 只应发送 Google HTTPS 链接；无效载荷不展示为可点击地址。
        }
      } else if (ev.status === 'ok' || ev.status === 'warn' || ev.status === 'error') {
        driveAuthorizationUrl.value = null
      }
    }
    if (ev.phase === 'log.line') {
      pushLog(String(ev.data?.text ?? ev.message ?? ''))
      return
    }
    if (ev.phase === 'metrics.sample') {
      pushMetric(ev)
      return
    }
    if (ev.phase === 'workflow.exec' && ev.status === 'progress') {
      progress.value = workflowProgress(ev)
      if (progress.value.text) pushLog(progress.value.text)
      return
    }
    applyMilestone(ev)
    if (!replay && TERMINAL_PHASES.has(ev.phase)) {
      terminal.value = true
    }
  }

  /** 手写 SSE 帧解析：': keepalive' 注释忽略，data 按空行分帧 */
  async function readStream(body: ReadableStream<Uint8Array>, controller: AbortController): Promise<void> {
    const reader = body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let dataLines: string[] = []
    let eventName = 'task'
    const dispatch = () => {
      if (dataLines.length === 0) return
      const raw = dataLines.join('\n')
      dataLines = []
      try {
        if (stopped || abortController !== controller) return
        if (eventName === 'snapshot') {
          const snapshot = JSON.parse(raw) as { status: string }
          terminal.value = !['queued', 'running'].includes(snapshot.status)
        } else {
          handleEvent(JSON.parse(raw) as TaskEvent, eventName === 'replay')
        }
      } catch {
        // 单帧解析失败不阻断流
      } finally {
        eventName = 'task'
      }
    }
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done || stopped || abortController !== controller) break
        lastFrameAt = Date.now()
        buffer += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, idx).replace(/\r$/, '')
          buffer = buffer.slice(idx + 1)
          if (line === '') {
            dispatch()
          } else if (line.startsWith('event:')) {
            eventName = line.slice(6).trim()
          } else if (line.startsWith(':')) {
            // keepalive 注释（doc/02 §2.5）
          } else if (line.startsWith('data:')) {
            dataLines.push(line.slice(5).replace(/^ /, ''))
          }
          // id 信息已包含在 data.seq；event 区分历史回放、快照与实时事件
        }
      }
      dispatch()
    } finally {
      reader.releaseLock()
    }
  }

  async function connect(): Promise<void> {
    if (stopped || !taskId.value) return
    abortController?.abort()
    const controller = new AbortController()
    abortController = controller
    lastFrameAt = Date.now()
    connectionState.value = 'connecting'

    const authStore = useAuthStore()
    const { $apiRefresh } = useNuxtApp()
    const url = `/api/tasks/${taskId.value}/events?from_seq=${lastSeq.value}`

    let res: Response
    try {
      res = await fetch(url, {
        headers: authStore.accessToken ? { Authorization: `Bearer ${authStore.accessToken}` } : {},
        signal: controller.signal,
      })
    } catch {
      if (stopped || abortController !== controller) return
      scheduleReconnect()
      return
    }

    if (stopped || abortController !== controller) {
      await res.body?.cancel()
      return
    }
    if (res.status === 401) {
      // 复用 apiFetch 插件的合流刷新，避免触发 token 重用保护
      const token = await $apiRefresh()
      if (stopped || abortController !== controller) return
      if (!token) {
        close()
        await navigateTo('/login')
        return
      }
      return connect() // 用新 token 立即重连，不计退避
    }
    if (res.status === 404) {
      errorMessage.value = '任务不存在或已被清理'
      connectionState.value = 'closed'
      return
    }
    if (!res.ok || !res.body) {
      scheduleReconnect()
      return
    }

    connectionState.value = 'open'
    errorMessage.value = ''
    backoffMs = BACKOFF_INIT_MS
    if (!logHistoryLoaded && taskId.value) {
      logHistoryLoaded = true
      void loadLogHistory(taskId.value, controller)
    }

    try {
      await readStream(res.body, controller)
    } catch {
      // 超时主动 abort 也需要重连；旧连接的退出不得影响新连接。
    }
    if (stopped || abortController !== controller) return
    if (flushTimer) {
      clearTimeout(flushTimer)
      flushLogs()
    }
    if (stopped) return
    // 流结束：终态（服务端回放完关闭 / 收到终态事件）→ 收工；否则按 from_seq 续订
    if (terminal.value) {
      connectionState.value = 'closed'
      return
    }
    scheduleReconnect()
  }

  function scheduleReconnect() {
    if (stopped || terminal.value || reconnectTimer) return
    connectionState.value = 'reconnecting'
    const delay = backoffMs
    backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX_MS)
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      void connect()
    }, delay)
  }

  /**
   * 打开任务事件流。已知终态的任务（opts.terminal=true）同样打开一次：
   * 服务端会回放 events.jsonl 后关闭（doc/02 §2.5），时间线由此构建。
   * SSE 订阅建立后统一加载 run.log，运行中刷新也可恢复历史日志。
   */
  function open(id: string, opts?: { terminal?: boolean }) {
    close()
    stopped = false
    taskId.value = id
    openedAt.value = Date.now()
    now.value = openedAt.value
    freshnessTimer = setInterval(() => {
      now.value = Date.now()
      if (!terminal.value && ['open', 'connecting'].includes(connectionState.value)
        && now.value - lastFrameAt > 45_000) {
        errorMessage.value = '实时通道超过 45 秒没有响应，正在重连'
        abortController?.abort()
      }
    }, 5_000)
    terminal.value = opts?.terminal ?? false
    void connect()
  }

  /** 拉 run.log 并与请求期间 SSE 收到的实时行去重合并。 */
  async function loadLogHistory(id: string, controller = abortController): Promise<void> {
    const authStore = useAuthStore()
    const { $apiRefresh } = useNuxtApp()
    const url = `/api/tasks/${id}/log`
    let res = await fetch(url, {
      headers: authStore.accessToken ? { Authorization: `Bearer ${authStore.accessToken}` } : {},
    }).catch(() => null)
    if (res?.status === 401) {
      const token = await $apiRefresh()
      if (token) {
        res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } }).catch(() => null)
      }
    }
    if (!res?.ok) return // 日志可能尚未创建（刚排队等），实时流仍会正常追加
    const text = await res.text()
    if (stopped || taskId.value !== id || controller !== abortController) return
    const historical = parseRunLog(text)
    const liveLines = linesRaw.slice()
    const liveOverwritable = rawOverwritable
    linesRaw = mergeLogHistory(historical.lines, liveLines, LOG_CAP)
    rawOverwritable = liveLines.length > 0
      ? liveOverwritable
      : historical.overwritable
    logLines.value = linesRaw.slice()
  }

  /** onUnmounted 必须调用（doc/04 §3.2）；浏览器断开不影响 Nitro↔Runner 订阅 */
  function close() {
    stopped = true
    if (freshnessTimer) clearInterval(freshnessTimer)
    freshnessTimer = null
    progress.value = null
    abortController?.abort()
    abortController = null
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (flushTimer) {
      clearTimeout(flushTimer)
      flushTimer = null
    }
    backoffMs = BACKOFF_INIT_MS
    logHistoryLoaded = false
    taskId.value = null
    connectionState.value = 'idle'
    terminal.value = false
    lastSeq.value = -1
    errorMessage.value = ''
    linesRaw = []
    rawOverwritable = false
    logLines.value = []
    metrics.value = []
    phases.value = []
    driveAuthorizationUrl.value = null
  }

  return {
    taskId,
    connectionState,
    terminal,
    lastSeq,
    errorMessage,
    progress,
    metricsStale,
    logLines,
    metrics,
    phases,
    driveAuthorizationUrl,
    open,
    close,
    loadLogHistory,
  }
})
