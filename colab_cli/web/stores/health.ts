import { defineStore } from 'pinia'

// 顶栏系统状态：轮询 GET /api/health（30s），显示 在线 / 降级 / 离线。
// 请求本身失败（Nitro 不可达）也视为离线。
export type HealthStatus = 'online' | 'degraded' | 'offline' | 'unknown'

export interface HealthCheckView {
  status: 'ok' | 'degraded' | 'down'
  message: string
  latencyMs?: number
}

export interface HealthResponse {
  status: Exclude<HealthStatus, 'unknown'>
  label: string
  checks: { db: HealthCheckView, runner: HealthCheckView, sessions: HealthCheckView }
  checkedAt: string
}

export const HEALTH_LABELS: Record<HealthStatus, string> = {
  online: '在线',
  degraded: '降级',
  offline: '离线',
  unknown: '检测中',
}

const CHECK_NAMES: Record<keyof HealthResponse['checks'], string> = {
  db: '数据库',
  runner: 'Runner',
  sessions: '会话',
}

/** 悬停提示：逐项列出检查结果 */
export function describeHealth(health: HealthResponse | null, error: string): string {
  if (error) return `无法获取系统状态：${error}`
  if (!health) return '正在检测系统状态…'
  return (Object.keys(CHECK_NAMES) as (keyof HealthResponse['checks'])[])
    .map(key => `${CHECK_NAMES[key]}：${health.checks[key].message}`)
    .join('\n')
}

const POLL_MS = 30_000

export const useHealthStore = defineStore('health', () => {
  const health = ref<HealthResponse | null>(null)
  const error = ref('')
  let timer: ReturnType<typeof setInterval> | null = null

  const status = computed<HealthStatus>(() => {
    if (error.value) return 'offline'
    return health.value?.status ?? 'unknown'
  })
  const label = computed(() => HEALTH_LABELS[status.value])
  const description = computed(() => describeHealth(health.value, error.value))

  async function refresh() {
    const { $apiFetch } = useNuxtApp()
    try {
      health.value = await $apiFetch<HealthResponse>('/api/health')
      error.value = ''
    } catch (err) {
      error.value = err instanceof Error ? err.message : '请求失败'
    }
  }

  function startPolling() {
    if (timer) return
    void refresh()
    timer = setInterval(() => void refresh(), POLL_MS)
  }

  function stopPolling() {
    if (timer) clearInterval(timer)
    timer = null
  }

  return { health, error, status, label, description, refresh, startPolling, stopPolling }
})
