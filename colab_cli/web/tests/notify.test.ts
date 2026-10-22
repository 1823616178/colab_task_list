import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createHmac } from 'node:crypto'
import {
  buildWebhookRequest,
  detectWebhookKind,
  notifyConfigFromRuntime,
  postWebhook,
  shouldNotify,
  summarizeError,
} from '../server/services/notify.ts'
import type { TaskNotifySummary } from '../server/services/notify.ts'
import { browserNotifyText, findLeftTasks } from '../utils/taskWatch.ts'

const FAILED: TaskNotifySummary = {
  id: '20260101-000000-abcdef12',
  status: 'failed',
  workflowName: 'MiniMax H3',
  sessionName: 's1',
  durationMs: '125000',
  errorCode: 'TransportError',
  errorMessage: 'line1\n   line2 ' + 'x'.repeat(500),
  outputSizeBytes: null,
}

test('按 URL 自动识别机器人类型', () => {
  assert.equal(detectWebhookKind('https://oapi.dingtalk.com/robot/send?access_token=x'), 'dingtalk')
  assert.equal(detectWebhookKind('https://open.feishu.cn/open-apis/bot/v2/hook/x'), 'feishu')
  assert.equal(detectWebhookKind('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x'), 'wecom')
  assert.equal(detectWebhookKind('https://example.com/hook'), 'generic')
})

test('未配置 URL → 关闭；notifyOn 过滤终态', () => {
  assert.equal(notifyConfigFromRuntime({ notifyWebhookUrl: '' }), null)
  const config = notifyConfigFromRuntime({ notifyWebhookUrl: 'https://example.com/h', notifyOn: 'failed' })
  assert.equal(shouldNotify(config, 'failed'), true)
  assert.equal(shouldNotify(config, 'succeeded'), false)
  const defaults = notifyConfigFromRuntime({ notifyWebhookUrl: 'https://example.com/h' })
  assert.deepEqual(defaults!.on, ['succeeded', 'failed', 'recoverable', 'orphaned'])
})

test('错误摘要单行化并截断', () => {
  const summary = summarizeError(FAILED.errorMessage)
  assert.ok(!summary.includes('\n'))
  assert.ok(summary.length <= 301)
})

test('钉钉：markdown + 加签参数', () => {
  const config = notifyConfigFromRuntime({
    notifyWebhookUrl: 'https://oapi.dingtalk.com/robot/send?access_token=t',
    notifyWebhookSecret: 'SEC',
    notifyConsoleUrl: 'https://console.example.com/',
  })!
  const request = buildWebhookRequest(config, FAILED, 1700000000000)
  const expected = createHmac('sha256', 'SEC').update('1700000000000\nSEC').digest('base64')
  assert.ok(request.url.includes('timestamp=1700000000000'))
  assert.ok(request.url.endsWith(`sign=${encodeURIComponent(expected)}`))
  const text = (request.body.markdown as { text: string }).text
  assert.match(text, /失败/)
  assert.match(text, /错误：TransportError - line1 line2/)
  assert.match(text, /2 分 5 秒/)
  assert.match(text, /https:\/\/console\.example\.com\/tasks\/20260101-000000-abcdef12/)
})

test('飞书 / 企业微信 / 通用 载荷格式', () => {
  const feishu = buildWebhookRequest(
    notifyConfigFromRuntime({ notifyWebhookUrl: 'https://open.feishu.cn/x', notifyWebhookSecret: 'S' })!,
    FAILED, 1700000000000)
  assert.equal(feishu.body.msg_type, 'text')
  assert.equal(feishu.body.timestamp, '1700000000')
  assert.equal(feishu.body.sign, createHmac('sha256', '1700000000\nS').update('').digest('base64'))
  const wecom = buildWebhookRequest(notifyConfigFromRuntime({ notifyWebhookUrl: 'https://qyapi.weixin.qq.com/x' })!, FAILED)
  assert.equal(wecom.body.msgtype, 'markdown')
  const generic = buildWebhookRequest(notifyConfigFromRuntime({ notifyWebhookUrl: 'https://e.com/x' })!, FAILED)
  assert.equal(generic.body.event, 'task.finished')
})

test('postWebhook：非 2xx 抛错', async () => {
  const fake = (async () => new Response('', { status: 500 })) as typeof fetch
  await assert.rejects(postWebhook({ url: 'https://e.com', body: {} }, fake), /HTTP 500/)
  const okFetch = (async () => new Response('{}', { status: 200 })) as typeof fetch
  await postWebhook({ url: 'https://e.com', body: {} }, okFetch)
})

test('浏览器通知：首轮只建基线，之后报告离开在途的任务', () => {
  assert.deepEqual(findLeftTasks(null, new Set(['a'])), [])
  assert.deepEqual(findLeftTasks(new Set(['a', 'b']), new Set(['b', 'c'])), ['a'])
  assert.equal(browserNotifyText({ id: 'a', status: 'running', workflowName: 'w', errorMessage: null }), null)
  const text = browserNotifyText({ id: 'a', status: 'failed', workflowName: 'w', errorMessage: 'boom' })
  assert.equal(text!.title, '任务失败：w')
  assert.match(text!.body, /boom/)
})
