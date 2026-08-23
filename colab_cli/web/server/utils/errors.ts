import { setResponseStatus } from 'h3'
import type { H3Event } from 'h3'

// doc/02 §3.4 统一错误格式：{ error: { code, message, detail } }
export interface ApiErrorBody {
  error: { code: string, message: string, detail: Record<string, unknown> }
}

export function sendApiError(
  event: H3Event,
  statusCode: number,
  code: string,
  message: string,
  detail: Record<string, unknown> = {},
): ApiErrorBody {
  setResponseStatus(event, statusCode)
  return { error: { code, message, detail } }
}
