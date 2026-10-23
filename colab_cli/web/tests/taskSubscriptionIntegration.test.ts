import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { deliverTaskEvent } from '../server/services/taskEventDelivery.ts'
import { loadService } from './helpers/loadService.ts'
import type * as Service from '../server/services/taskSubscription.ts'
import type { RunnerTaskEvent } from '../server/services/taskEvents.ts'

const event = (seq: number, phase: string): RunnerTaskEvent => ({
  v: 1, task_id: 'task', seq, phase, status: phase === 'task.done' ? 'ok' : 'start', ts: '2026-10-22T17:00:00Z', message: '', data: {},
})
const frame = (e: RunnerTaskEvent) => `data: ${JSON.stringify(e)}\n\n`

function harness(stream: (path: string) => Promise<Response>, commit: (e: RunnerTaskEvent) => Promise<void>) {
  let notifications = 0
  const service = loadService<typeof Service>(new URL('../server/services/taskSubscription.ts', import.meta.url), {
    typeorm: { In: (v: unknown) => v },
    './taskEventDelivery': { deliverTaskEvent },
    '../entities/Task': { TaskSchema: 'task' },
    './taskNotify': { notifyTaskFinished: () => { notifications++ } },
    '../utils/db': {},
    './runnerClient': { runnerStream: stream },
    './taskEvents': {
      isStorableEvent: () => true,
      isTerminalEvent: (e: RunnerTaskEvent) => e.phase === 'task.done',
      persistMilestoneEvent: commit,
      upsertModelCacheFromEvent: async () => {},
    },
    './ftpUpload': { scheduleTaskFtpUpload: async () => {} },
  }, {
    setTimeout: (cb: () => void, ms: number) => {
      if (ms !== 45000) queueMicrotask(cb)
      return { unref() {} }
    },
    clearTimeout: () => {},
  })
  return { service, notifications: () => notifications }
}

test('真实订阅循环在终态入库失败后从已提交水位重连，最终广播一次终态', async () => {
  const calls: string[] = []
  const writes: number[] = []
  const h = harness(async (path) => {
    calls.push(path)
    const events = calls.length === 1 ? [event(81, 'output.transfer'), event(82, 'task.done')] : [event(82, 'task.done')]
    return new Response(events.map(frame).join(''))
  }, async (e) => {
    writes.push(e.seq)
    if (e.seq === 82 && writes.filter(n => n === 82).length === 1) throw Error('DB unavailable')
  })
  const received: string[] = []
  h.service.subscribeBrowser('task', frame => received.push(frame))
  h.service.ensureTaskSubscription('task', 80)
  for (let i = 0; i < 20 && h.service.hasTaskSubscription('task'); i++) await nextTurn()
  assert.equal(h.service.hasTaskSubscription('task'), false)
  assert.deepEqual(calls, ['/tasks/task/events?from_seq=80', '/tasks/task/events?from_seq=81'])
  assert.deepEqual(writes, [81, 82, 82])
  assert.equal(received.length, 2)
  assert.equal(received.filter(f => f.includes('task.done')).length, 1)
  assert.equal(h.notifications(), 1)
})
