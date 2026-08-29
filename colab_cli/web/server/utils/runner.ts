import { readBody } from 'h3'
import type { H3Event } from 'h3'
import { RunnerApiError, RunnerUnavailableError } from '../services/runnerClient'
import { sendApiError } from '../utils/errors'
import type { ApiErrorBody } from '../utils/errors'

// Runner 客户端错误 → 统一 API 错误（doc/02 §3.4）：
// RunnerUnavailableError → 503 RUNNER_UNAVAILABLE；Runner 业务错误原码原样透传。
export function sendRunnerError(event: H3Event, error: unknown): ApiErrorBody {
  if (error instanceof RunnerApiError) {
    return sendApiError(event, error.status, error.code, error.message, error.detail)
  }
  if (error instanceof RunnerUnavailableError) {
    return sendApiError(event, 503, 'RUNNER_UNAVAILABLE', 'Runner 未启动或健康检查失败', { reason: error.message })
  }
  throw error
}

/** 读取可选 JSON body（空 body → {}），非法 JSON → 400。 */
export async function readOptionalJsonBody(event: H3Event): Promise<Record<string, unknown> | ApiErrorBody> {
  try {
    const body = await readBody(event)
    if (body === undefined || body === null) return {}
    if (typeof body !== 'object' || Array.isArray(body)) {
      return sendApiError(event, 400, 'BAD_REQUEST', '请求体必须是 JSON 对象')
    }
    return body as Record<string, unknown>
  } catch {
    return sendApiError(event, 400, 'BAD_REQUEST', '请求体不是合法 JSON')
  }
}

export function isApiError(value: unknown): value is ApiErrorBody {
  return !!value && typeof value === 'object' && 'error' in value
}
