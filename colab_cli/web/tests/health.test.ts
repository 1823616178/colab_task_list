import { test } from 'node:test'
import assert from 'node:assert/strict'
import { aggregateHealth, timedCheck } from '../server/services/health.ts'
import type { HealthCheck } from '../server/services/health.ts'

const ok: HealthCheck = { status: 'ok', message: 'ok' }
const degraded: HealthCheck = { status: 'degraded', message: 'd' }
const down: HealthCheck = { status: 'down', message: 'x' }

test('全部正常 → 在线', () => {
  assert.equal(aggregateHealth({ db: ok, runner: ok, sessions: ok }), 'online')
})

test('DB 不可用 → 离线；Runner 不可用或有孤儿 → 降级', () => {
  assert.equal(aggregateHealth({ db: down, runner: ok, sessions: ok }), 'offline')
  assert.equal(aggregateHealth({ db: ok, runner: down, sessions: degraded }), 'degraded')
  assert.equal(aggregateHealth({ db: ok, runner: ok, sessions: degraded }), 'degraded')
})

test('timedCheck：异常与超时都转为 down 并带耗时', async () => {
  const failed = await timedCheck('x', async () => { throw new Error('boom') })
  assert.equal(failed.status, 'down')
  assert.equal(failed.message, 'boom')
  const slow = await timedCheck('慢检查', () => new Promise<HealthCheck>(() => {}), 20)
  assert.equal(slow.status, 'down')
  assert.match(slow.message, /超时/)
  const fine = await timedCheck('y', async () => ok)
  assert.equal(fine.status, 'ok')
  assert.equal(typeof fine.latencyMs, 'number')
})
