import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setImmediate as nextTurn } from 'node:timers/promises'
import * as monitoring from '../utils/taskMonitoring.ts'
import * as logHistory from '../utils/logHistory.ts'
import { loadService } from './helpers/loadService.ts'

type Ref<T> = { value: T }
interface Store {
  open: (id: string) => void
  close: () => void
  progress: Ref<monitoring.WorkflowProgress | null>
  terminal: Ref<boolean>
  connectionState: Ref<string>
  errorMessage: Ref<string>
  logLines: Ref<string[]>
  metricsStale: Ref<boolean>
}

function harness() {
  let clock = Date.parse('2026-10-22T17:00:00Z')
  let tick = () => {}
  let controller: ReadableStreamDefaultController<Uint8Array>
  let aborted = false
  const timers = new Map<number, () => void>()
  const Clock = class extends Date { static override now() { return clock } }
  const body = new ReadableStream<Uint8Array>({ start: c => { controller = c } })
  const ref = <T>(value: T): Ref<T> => ({ value })
  const service = loadService<{ useTaskStreamStore: () => Store }>(new URL('../stores/taskStream.ts', import.meta.url), {
    pinia: { defineStore: (_id: string, setup: () => Store) => setup },
    '~/utils/taskMonitoring': monitoring,
    '~/utils/logHistory': logHistory,
  }, {
    ref, shallowRef: ref, computed: (get: () => unknown) => ({ get value() { return get() } }),
    useAuthStore: () => ({ accessToken: 'test' }),
    useNuxtApp: () => ({ $apiRefresh: async () => null }),
    navigateTo: async () => {},
    Date: Clock,
    setInterval: (callback: () => void) => { tick = callback; return 1 },
    clearInterval: () => {},
    setTimeout: (callback: () => void, ms: number) => { timers.set(ms, callback); return ms },
    clearTimeout: (ms: number) => timers.delete(ms),
    fetch: async (url: string, opts: { signal?: AbortSignal }) => {
      if (url.endsWith('/log')) return new Response('')
      opts.signal?.addEventListener('abort', () => { aborted = true; controller.error(new Error('aborted')) }, { once: true })
      return new Response(body)
    },
  })
  return {
    store: service.useTaskStreamStore(), timers,
    send: (type: string, data: object) => controller.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`)),
    finish: () => controller.close(),
    advance: (ms: number) => { clock += ms; tick() },
    aborted: () => aborted,
  }
}

const event = (seq: number, phase: string, status: string, data = {}) => ({
  task_id: 'task', seq, phase, status, ts: '2026-10-22T17:00:00Z', message: '节点 2/4 (50%)', data,
})

test('真实前端 Store 忽略历史终态，展示实时节点进度，并正常接收新终态', async () => {
  const h = harness()
  h.store.open('task')
  await nextTurn()
  h.send('replay', event(5, 'task.failed', 'error'))
  h.send('snapshot', { status: 'running', lastSeq: 5 })
  h.send('task', event(6, 'workflow.exec', 'progress', { current_node: 'KSampler', step: 2, steps: 4 }))
  await nextTurn()
  assert.equal(h.store.terminal.value, false)
  assert.equal(h.store.progress.value?.currentNode, 'KSampler')
  assert.equal(h.store.progress.value?.step, 2)
  h.timers.get(150)?.()
  assert.ok(h.store.logLines.value.some(text => text.includes('2/4')))
  h.send('task', event(7, 'task.done', 'ok'))
  h.finish()
  await nextTurn()
  assert.equal(h.store.terminal.value, true)
  assert.equal(h.store.connectionState.value, 'closed')
})

test('真实前端 Store 对无响应连接主动中止并重连，旧资源数据提示过期', async () => {
  const h = harness()
  h.store.open('task')
  await nextTurn()
  h.advance(45001)
  await nextTurn()
  assert.equal(h.aborted(), true)
  assert.equal(h.store.connectionState.value, 'reconnecting')
  assert.ok(h.timers.has(1000))
  h.advance(50000)
  assert.equal(h.store.metricsStale.value, true)
  h.store.close()
  assert.equal(h.store.connectionState.value, 'idle')
})
