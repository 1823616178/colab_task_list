import { defineStore } from 'pinia'
import { extractApiError } from '~/stores/auth'

// doc/04 §3.3：session 会话列表、对账结果、孤儿告警。
// P4 落地：GET /api/sessions 的聚合形状（Runner 实况 ⋈ sessions 表 ⋈ 在途任务）、
// 30s 轮询（≤60s 要求）、orphans 计算属性驱动 app.vue 全局横幅、停止/对账动作。

/** GET /api/sessions 的聚合条目（server/services/sessionReconcileRules.SessionView 序列化后） */
export interface SessionItem {
  name: string
  /** false 表示显示名由数据库按 endpoint 找回，Runner 已无法按名称停止。 */
  runnerNameKnown: boolean
  /** 服务器端确认活着（正在计费） */
  reachable: boolean
  /** IDLE | BUSY | UNKNOWN；不在 Runner 列表为 null */
  runnerStatus: string | null
  variant: string | null
  accelerator: string | null
  endpoint: string | null
  inDb: boolean
  dbStatus: 'idle' | 'busy' | 'unreachable' | 'stopped' | null
  expectedAlive: boolean
  /** UTC ISO 串；createdAt = 首次对账见到的时间（近似 VM 创建时间） */
  createdAt: string | null
  lastSeenAt: string | null
  stoppedAt: string | null
  reconciledAt: string | null
  hasRunningTask: boolean
  runningTask: { id: string, workflowName: string, status: string } | null
  /** 孤儿：reachable && !expectedAlive && !hasRunningTask（白烧计算单元） */
  isOrphan: boolean
}

export interface ReconcileResult {
  ok: true
  reconciledAt: string
  alive: string[]
  orphans: string[]
  gone: { name: string, status: 'unreachable' | 'stopped' }[]
  stopped: string[]
  orphanedTasks: { taskId: string, sessionName: string }[]
  expiredRecoveries: {
    taskId: string
    sessionName: string
    status: 'failed' | 'canceled'
  }[]
}

export interface CreateSessionPayload {
  name: string
  accelerator: { kind: 'gpu' | 'tpu', spec: string }
  mount_drive?: boolean
}

export interface SessionOpResult {
  ok: boolean
  name?: string
  endpoint?: string
  log?: string
}

const POLL_INTERVAL_MS = 30_000

export const useSessionStore = defineStore('session', () => {
  const sessions = ref<SessionItem[]>([])
  const loading = ref(false)
  /** 最近一次拉取失败的原因（Runner 不可达等）；成功清空 */
  const lastError = ref<string | null>(null)
  const fetchedAt = ref<string | null>(null)

  // doc/04 §3.3：孤儿告警公式（全局横幅在 app.vue 层，不只 /sessions 页）
  const orphans = computed(() =>
    sessions.value.filter(s => s.reachable && !s.expectedAlive && !s.hasRunningTask))

  async function fetchSessions(): Promise<void> {
    const { $apiFetch } = useNuxtApp()
    loading.value = true
    try {
      const res = await $apiFetch<{ items: SessionItem[], generatedAt: string }>('/api/sessions')
      sessions.value = res.items
      fetchedAt.value = res.generatedAt
      lastError.value = null
    } catch (err) {
      // Runner 不可达（503）等：实况不可知，清空避免陈旧数据误报孤儿
      sessions.value = []
      lastError.value = extractApiError(err, '加载会话列表失败')
      throw err
    } finally {
      loading.value = false
    }
  }

  // ── 轮询（app.vue 登录期间启动；30s ≤ 60s 要求）────────────────
  let pollTimer: ReturnType<typeof setInterval> | null = null

  function startPolling(): void {
    if (pollTimer) return // 防重复注册
    void fetchSessions().catch(() => {})
    pollTimer = setInterval(() => {
      void fetchSessions().catch(() => {})
    }, POLL_INTERVAL_MS)
  }

  function stopPolling(): void {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  // ── 动作（operator+；API 侧另有 role 守卫）────────────────────
  async function createSession(payload: CreateSessionPayload): Promise<SessionOpResult> {
    const { $apiFetch } = useNuxtApp()
    const result = await $apiFetch<SessionOpResult>('/api/sessions', {
      method: 'POST',
      body: payload,
      timeout: 180_000,
    })
    await fetchSessions()
    return result
  }

  async function stopSession(name: string): Promise<SessionOpResult> {
    const { $apiFetch } = useNuxtApp()
    const result = await $apiFetch<SessionOpResult>(
      `/api/sessions/${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    )
    await fetchSessions()
    return result
  }

  /** 名称映射丢失（Runner 侧名称为 "?"）时按 endpoint 停止 */
  async function stopSessionByEndpoint(endpoint: string): Promise<SessionOpResult> {
    const { $apiFetch } = useNuxtApp()
    const result = await $apiFetch<SessionOpResult>(
      `/api/sessions/by-endpoint/${encodeURIComponent(endpoint)}`,
      { method: 'DELETE' },
    )
    await fetchSessions()
    return result
  }

  /** 手动触发一轮对账（operator+） */
  async function reconcile(): Promise<ReconcileResult> {
    const { $apiFetch } = useNuxtApp()
    const result = await $apiFetch<ReconcileResult>('/api/sessions/reconcile', { method: 'POST' })
    await fetchSessions()
    return result
  }

  return {
    sessions,
    loading,
    lastError,
    fetchedAt,
    orphans,
    fetchSessions,
    startPolling,
    stopPolling,
    createSession,
    stopSession,
    stopSessionByEndpoint,
    reconcile,
  }
})
