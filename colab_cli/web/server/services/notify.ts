// 任务终态通知：可配置 Webhook（钉钉 / 飞书 / 企业微信 / 通用 JSON）。
// 铁律：通知失败绝不影响任务状态回写 —— 只 console.warn。
// 配置（web/.env）：
//   NUXT_NOTIFY_WEBHOOK_URL     机器人地址；留空 = 关闭
//   NUXT_NOTIFY_WEBHOOK_KIND    dingtalk | feishu | wecom | generic（缺省按 URL 自动识别）
//   NUXT_NOTIFY_WEBHOOK_SECRET  钉钉/飞书「加签」密钥（可选）
//   NUXT_NOTIFY_ON              逗号分隔的终态，缺省 succeeded,failed,recoverable,orphaned
//   NUXT_NOTIFY_CONSOLE_URL     控制台外网地址，用于消息里的任务详情链接（可选）
import { createHmac } from 'node:crypto'

export type WebhookKind = 'dingtalk' | 'feishu' | 'wecom' | 'generic'

export interface NotifyConfig {
  url: string
  kind: WebhookKind
  secret: string
  on: string[]
  consoleUrl: string
}

export interface TaskNotifySummary {
  id: string
  status: string
  workflowName: string
  sessionName: string
  durationMs: string | number | null
  errorCode: string | null
  errorMessage: string | null
  outputSizeBytes: string | number | null
}

export const DEFAULT_NOTIFY_ON = ['succeeded', 'failed', 'recoverable', 'orphaned']

const STATUS_TEXT: Record<string, string> = {
  succeeded: '✅ 成功',
  failed: '❌ 失败',
  recoverable: '⚠️ 失败（VM 存活，可重取）',
  canceled: '⏹ 已取消',
  orphaned: '⚠️ 已失联',
}

export function detectWebhookKind(url: string): WebhookKind {
  if (url.includes('oapi.dingtalk.com')) return 'dingtalk'
  if (url.includes('open.feishu.cn') || url.includes('open.larksuite.com')) return 'feishu'
  if (url.includes('qyapi.weixin.qq.com')) return 'wecom'
  return 'generic'
}

export function notifyConfigFromRuntime(config: Record<string, unknown>): NotifyConfig | null {
  const url = String(config.notifyWebhookUrl ?? '').trim()
  if (!url) return null
  const rawKind = String(config.notifyWebhookKind ?? '').trim().toLowerCase()
  const kind = (['dingtalk', 'feishu', 'wecom', 'generic'].includes(rawKind)
    ? rawKind
    : detectWebhookKind(url)) as WebhookKind
  const onRaw = String(config.notifyOn ?? '').trim()
  return {
    url,
    kind,
    secret: String(config.notifyWebhookSecret ?? ''),
    on: onRaw ? onRaw.split(',').map(item => item.trim()).filter(Boolean) : DEFAULT_NOTIFY_ON,
    consoleUrl: String(config.notifyConsoleUrl ?? '').replace(/\/+$/, ''),
  }
}

function formatDuration(ms: string | number | null): string {
  const value = Number(ms)
  if (!Number.isFinite(value) || value <= 0) return '—'
  const minutes = Math.floor(value / 60_000)
  const seconds = Math.round((value % 60_000) / 1000)
  return minutes > 0 ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`
}

/** 错误摘要：单行化并截断，避免把整段堆栈刷进群里 */
export function summarizeError(message: string | null, maxLength = 300): string {
  if (!message) return ''
  const oneLine = message.replace(/\s+/g, ' ').trim()
  return oneLine.length > maxLength ? `${oneLine.slice(0, maxLength)}…` : oneLine
}

export function buildNotifyText(task: TaskNotifySummary, consoleUrl = ''): { title: string, lines: string[] } {
  const title = `Colab 任务${STATUS_TEXT[task.status] ?? task.status}：${task.workflowName}`
  const lines = [
    `任务 ID：${task.id}`,
    `会话：${task.sessionName}`,
    `耗时：${formatDuration(task.durationMs)}`,
  ]
  if (task.status === 'succeeded' && task.outputSizeBytes) {
    lines.push(`产物大小：${(Number(task.outputSizeBytes) / 1024 ** 2).toFixed(1)} MB`)
  }
  const error = summarizeError(task.errorMessage)
  if (task.status !== 'succeeded' && (task.errorCode || error)) {
    lines.push(`错误：${[task.errorCode, error].filter(Boolean).join(' - ')}`)
  }
  if (task.status === 'recoverable') lines.push('提示：VM 仍在计费，请尽快到详情页重试下载或停止会话')
  if (consoleUrl) lines.push(`详情：${consoleUrl}/tasks/${task.id}`)
  return { title, lines }
}

export interface WebhookRequest {
  url: string
  body: Record<string, unknown>
}

/** 钉钉加签：URL 追加 timestamp & sign（HmacSHA256(secret, `${ts}\n${secret}`)） */
function dingtalkSignedUrl(url: string, secret: string, nowMs: number): string {
  const stringToSign = `${nowMs}\n${secret}`
  const sign = createHmac('sha256', secret).update(stringToSign).digest('base64')
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}timestamp=${nowMs}&sign=${encodeURIComponent(sign)}`
}

/** 飞书加签：key = `${ts}\n${secret}`，对空串做 HmacSHA256（秒级时间戳） */
function feishuSign(secret: string, nowSec: number): string {
  return createHmac('sha256', `${nowSec}\n${secret}`).update('').digest('base64')
}

export function buildWebhookRequest(
  config: NotifyConfig,
  task: TaskNotifySummary,
  nowMs: number = Date.now(),
): WebhookRequest {
  const { title, lines } = buildNotifyText(task, config.consoleUrl)
  switch (config.kind) {
    case 'dingtalk':
      return {
        url: config.secret ? dingtalkSignedUrl(config.url, config.secret, nowMs) : config.url,
        body: {
          msgtype: 'markdown',
          markdown: { title, text: `### ${title}\n\n${lines.map(line => `- ${line}`).join('\n')}` },
        },
      }
    case 'feishu': {
      const body: Record<string, unknown> = {
        msg_type: 'text',
        content: { text: `${title}\n${lines.join('\n')}` },
      }
      if (config.secret) {
        const ts = Math.floor(nowMs / 1000)
        body.timestamp = String(ts)
        body.sign = feishuSign(config.secret, ts)
      }
      return { url: config.url, body }
    }
    case 'wecom':
      return {
        url: config.url,
        body: { msgtype: 'markdown', markdown: { content: `**${title}**\n${lines.map(line => `> ${line}`).join('\n')}` } },
      }
    default:
      return { url: config.url, body: { event: 'task.finished', title, lines, task } }
  }
}

export function shouldNotify(config: NotifyConfig | null, status: string): config is NotifyConfig {
  return config !== null && config.on.includes(status)
}

export async function postWebhook(
  request: WebhookRequest,
  fetchImpl: typeof fetch = fetch,
  timeoutMs = 5_000,
): Promise<void> {
  const response = await fetchImpl(request.url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request.body),
    signal: AbortSignal.timeout(timeoutMs),
  })
  if (!response.ok) throw new Error(`Webhook 返回 HTTP ${response.status}`)
}
