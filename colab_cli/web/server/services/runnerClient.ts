// Runner 客户端封装（doc/02 §2/§3.4）。
//
// 纪律：
// - 普通调用带超时（默认 10s）；**幂等 GET 网络错误/5xx 重试一次，POST 绝不重试**
//   （POST /tasks 不是幂等的，重试可能重复受理）。
// - Runner 不可达 / 健康检查失败 → 抛 RunnerUnavailableError，API 层统一映射为
//   503 RUNNER_UNAVAILABLE。
// - Runner 返回的业务错误（已是 {error:{code,message,detail}} 格式）原样透传，
//   抛 RunnerApiError 由 API 层映射同码同消息。
// - SSE 流（/events）不走这里 —— 见 services/taskSubscription.ts（长连接，
//   无超时，自行指数退避重连）。

export class RunnerUnavailableError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'RunnerUnavailableError'
  }
}

export class RunnerApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public detail: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'RunnerApiError'
  }
}

export interface RunnerRequestOptions {
  method?: 'GET' | 'POST' | 'DELETE'
  body?: unknown
  /** 默认 10s；健康检查用更短 */
  timeoutMs?: number
}

const DEFAULT_TIMEOUT_MS = 10_000
const GET_RETRY_DELAY_MS = 500
const RETRYABLE_STATUSES = new Set([502, 503, 504])

function runnerBase() {
  const config = useRuntimeConfig()
  const url = String(config.runnerUrl || '').replace(/\/+$/, '')
  const token = String(config.runnerToken || '')
  if (!url || !token) {
    throw new RunnerUnavailableError('未配置 NUXT_RUNNER_URL / NUXT_RUNNER_TOKEN')
  }
  return { url, token }
}

async function requestOnce<T>(path: string, options: RunnerRequestOptions): Promise<T> {
  const { url, token } = runnerBase()
  let response: Response
  try {
    response = await fetch(`${url}${path}`, {
      method: options.method ?? 'GET',
      headers: {
        'X-Internal-Token': token,
        ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: AbortSignal.timeout(options.timeoutMs ?? DEFAULT_TIMEOUT_MS),
    })
  } catch (error) {
    throw new RunnerUnavailableError(
      `Runner 不可达: ${error instanceof Error ? error.message : String(error)}`,
    )
  }
  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }
  if (!response.ok) {
    const error = (payload as { error?: { code?: string, message?: string, detail?: Record<string, unknown> } } | null)?.error
    throw new RunnerApiError(
      response.status,
      error?.code ?? 'RUNNER_ERROR',
      error?.message ?? `Runner 返回 HTTP ${response.status}`,
      error?.detail ?? {},
    )
  }
  return payload as T
}

/** 普通 Runner 调用：GET（幂等）失败重试一次，POST/DELETE 不重试。 */
export async function runnerRequest<T>(path: string, options: RunnerRequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET'
  try {
    return await requestOnce<T>(path, options)
  } catch (error) {
    const retryable =
      method === 'GET'
      && (error instanceof RunnerUnavailableError
        || (error instanceof RunnerApiError && RETRYABLE_STATUSES.has(error.status)))
    if (!retryable) throw error
    await new Promise(resolve => setTimeout(resolve, GET_RETRY_DELAY_MS))
    return await requestOnce<T>(path, options)
  }
}

export interface RunnerHealth {
  ok: boolean
  version: string
  transport: { mode: string, degraded?: boolean, probe: Record<string, boolean> }
  running_tasks: number
}

/**
 * 健康检查（5s 超时，不重试）。失败抛 RunnerUnavailableError → 503。
 * transport probe 任一为 false 时 Runner 已自动降级 cli（doc/02 §2.1），
 * 不算不可用，但返回值里带着，调用方负责告警。
 */
export async function checkRunnerHealth(): Promise<RunnerHealth> {
  return await requestOnce<RunnerHealth>('/health', { timeoutMs: 5_000 })
}

/** 供 SSE 订阅管理器使用的原始流式 GET（无超时，AbortController 自持）。 */
export function runnerStream(path: string, signal: AbortSignal): Promise<Response> {
  const { url, token } = runnerBase()
  return fetch(`${url}${path}`, {
    headers: { 'X-Internal-Token': token, Accept: 'text/event-stream' },
    signal,
  })
}
