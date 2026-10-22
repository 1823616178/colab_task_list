import { test } from 'node:test'
import assert from 'node:assert/strict'
import { LoginRateLimiter } from '../server/utils/loginRateLimit.ts'

const OPTS = {
  windowMs: 60_000,
  maxFailures: 5,
  maxFailuresPerIp: 8,
  lockoutMs: 15 * 60_000,
  delayBaseMs: 100,
  maxDelayMs: 1_000,
}

function make() {
  let now = 1_000_000
  const limiter = new LoginRateLimiter(OPTS, () => now)
  return { limiter, advance: (ms: number) => { now += ms } }
}

test('窗口内第 5 次失败触发锁定，锁定期内拒绝', () => {
  const { limiter, advance } = make()
  for (let i = 0; i < 4; i++) {
    assert.equal(limiter.recordFailure('1.1.1.1', 'admin').locked, false)
  }
  assert.equal(limiter.check('1.1.1.1', 'admin').allowed, true)
  assert.equal(limiter.recordFailure('1.1.1.1', 'admin').locked, true)
  const verdict = limiter.check('1.1.1.1', 'admin')
  assert.equal(verdict.allowed, false)
  assert.equal(verdict.retryAfterMs, 15 * 60_000)
  // 其他用户名、其他 IP 不受影响
  assert.equal(limiter.check('1.1.1.1', 'bob').allowed, true)
  assert.equal(limiter.check('2.2.2.2', 'admin').allowed, true)
  advance(15 * 60_000)
  assert.equal(limiter.check('1.1.1.1', 'admin').allowed, true)
})

test('滑动窗口：超过窗口的旧失败不计数', () => {
  const { limiter, advance } = make()
  for (let i = 0; i < 4; i++) limiter.recordFailure('1.1.1.1', 'admin')
  advance(61_000)
  assert.equal(limiter.recordFailure('1.1.1.1', 'admin').locked, false)
})

test('渐进延迟按失败次数翻倍并封顶', () => {
  const { limiter } = make()
  const delays = [1, 2, 3, 4].map(() => limiter.recordFailure('1.1.1.1', 'admin').delayMs)
  assert.deepEqual(delays, [100, 200, 400, 800])
})

test('登录成功清空失败计数；用户名大小写视为同一账号', () => {
  const { limiter } = make()
  for (let i = 0; i < 4; i++) limiter.recordFailure('1.1.1.1', 'Admin')
  limiter.recordSuccess('1.1.1.1', 'admin')
  assert.equal(limiter.recordFailure('1.1.1.1', 'admin').locked, false)
})

test('同一 IP 跨用户名撞库达到上限 → 整个 IP 锁定', () => {
  const { limiter } = make()
  let locked = false
  for (let i = 0; i < 8; i++) locked = limiter.recordFailure('9.9.9.9', `user${i}`).locked
  assert.equal(locked, true)
  assert.equal(limiter.check('9.9.9.9', 'someone-else').allowed, false)
})
