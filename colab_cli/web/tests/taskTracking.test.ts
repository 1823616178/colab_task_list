import { test } from 'node:test'
import assert from 'node:assert/strict'
import { sessionLostResumeStatus, verifyTaskTracking } from '../server/services/taskTracking.ts'
import type { RunnerTaskSnapshot } from '../server/services/taskTracking.ts'

const TASK = {
  id: '20261022-164200-a45d45c2',
  sessionName: 'comfyui-h3',
  status: 'orphaned',
  errorCode: 'SESSION_LOST',
  lastSeq: 80,
}

const SNAPSHOT: RunnerTaskSnapshot = {
  task_id: TASK.id,
  session: TASK.sessionName,
  status: 'running',
  phase: 'workflow.exec',
  last_seq: 80,
}

test('会话查询缺项时，Runner 仍有在途或终态记录就保留订阅', async () => {
  for (const status of ['queued', 'running', 'redownloading', 'succeeded', 'failed', 'canceled']) {
    const result = await verifyTaskTracking(TASK, async () => ({ ...SNAPSHOT, status }))
    assert.equal(result.kind, 'tracked')
  }
})

test('仅明确的任务 404 允许标记失联；超时、5xx 和无效响应都保留状态', async () => {
  for (const [error, expected] of [
    [{ status: 404 }, 'missing'],
    [{ status: 503 }, 'unavailable'],
    [new Error('timeout'), 'unavailable'],
  ] as const) {
    assert.equal((await verifyTaskTracking(TASK, async () => { throw error })).kind, expected)
  }
  for (const snapshot of [
    { ...SNAPSHOT, task_id: 'another-task' },
    { ...SNAPSHOT, session: 'another-session' },
    { ...SNAPSHOT, status: 'unknown' },
    { ...SNAPSHOT, last_seq: undefined },
  ]) {
    const result = await verifyTaskTracking(TASK, async () => snapshot as RunnerTaskSnapshot)
    assert.equal(result.kind, 'unavailable')
  }
})

test('误标 SESSION_LOST 的原任务可恢复，已完成的任务通过补拉终态收尾', async () => {
  for (const [runnerStatus, expected] of [['queued', 'queued'], ['running', 'running'], ['succeeded', 'running']] as const) {
    const tracking = await verifyTaskTracking(TASK, async () => ({ ...SNAPSHOT, status: runnerStatus }))
    assert.equal(sessionLostResumeStatus(TASK, tracking), expected)
  }
})

test('真实终态、其他失联原因、旧记录和会话不匹配都不会被重新打开', async () => {
  const tracking = await verifyTaskTracking(TASK, async () => SNAPSHOT)
  for (const status of ['running', 'succeeded', 'failed', 'recoverable', 'canceled']) {
    assert.equal(sessionLostResumeStatus({ ...TASK, status }, tracking), null)
  }
  assert.equal(sessionLostResumeStatus({ ...TASK, errorCode: 'RUNNER_LOST' }, tracking), null)
  assert.equal(sessionLostResumeStatus({ ...TASK, lastSeq: 81 }, tracking), null)
  assert.equal(sessionLostResumeStatus({ ...TASK, sessionName: 'another-session' }, tracking), null)
  assert.equal(sessionLostResumeStatus(TASK, { kind: 'missing' }), null)
  assert.equal(sessionLostResumeStatus(TASK, { kind: 'interrupted' }), null)
  assert.equal(sessionLostResumeStatus(TASK, { kind: 'unavailable' }), null)
})

test('Runner 重启后明确 interrupted 的记录不当作仍在执行，也不自动恢复', async () => {
  const tracking = await verifyTaskTracking(TASK, async () => ({ ...SNAPSHOT, status: 'interrupted' }))
  assert.equal(tracking.kind, 'interrupted')
  assert.equal(sessionLostResumeStatus(TASK, tracking), null)
})
