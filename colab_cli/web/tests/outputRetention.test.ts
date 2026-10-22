import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  retentionFor,
  retentionOptionsFromRuntime,
  shouldDeleteOutput,
} from '../server/services/outputRetention.ts'
import type { RetentionTaskView } from '../server/services/outputRetention.ts'

const OPTS = { retentionDays: 14, unsyncedRetentionDays: 60 }
const FINISHED = new Date('2026-01-01T00:00:00Z')
const DAY = 86_400_000

function task(overrides: Partial<RetentionTaskView> = {}): RetentionTaskView {
  return {
    status: 'succeeded',
    finishedAt: FINISHED,
    keepOutput: false,
    ftpStatus: 'uploaded',
    outputZipPath: '/out/x/output.zip',
    outputDownloadedAt: null,
    transportMode: 'native',
    ...overrides,
  }
}

test('已同步 FTP 的成功产物按标准保留期', () => {
  const info = retentionFor(task(), OPTS)
  assert.equal(info.reason, 'standard')
  assert.equal(info.expiresAt!.getTime(), FINISHED.getTime() + 14 * DAY)
  assert.equal(shouldDeleteOutput(task(), OPTS, new Date(FINISHED.getTime() + 15 * DAY)), true)
})

test('FTP 失败且从未下载 → 延长保留，不在标准期删除', () => {
  for (const ftpStatus of ['failed', 'pending', 'uploading', null]) {
    const t = task({ ftpStatus })
    assert.equal(retentionFor(t, OPTS).reason, 'unsynced')
    assert.equal(shouldDeleteOutput(t, OPTS, new Date(FINISHED.getTime() + 15 * DAY)), false)
    assert.equal(shouldDeleteOutput(t, OPTS, new Date(FINISHED.getTime() + 61 * DAY)), true)
  }
})

test('已下载过的产物即使 FTP 失败也回落标准保留期', () => {
  const t = task({ ftpStatus: 'failed', outputDownloadedAt: new Date() })
  assert.equal(retentionFor(t, OPTS).reason, 'standard')
})

test('keepOutput 永不清理；非终态不清理', () => {
  assert.equal(retentionFor(task({ keepOutput: true }), OPTS).expiresAt, null)
  assert.equal(shouldDeleteOutput(task({ keepOutput: true }), OPTS, new Date('2100-01-01')), false)
  assert.equal(retentionFor(task({ status: 'running', finishedAt: null }), OPTS).reason, 'not_terminal')
})

test('失败任务 / 演练任务按标准保留期', () => {
  assert.equal(retentionFor(task({ status: 'failed', outputZipPath: null }), OPTS).reason, 'standard')
  assert.equal(retentionFor(task({ transportMode: 'dry_run', ftpStatus: null }), OPTS).reason, 'standard')
})

test('运行时配置：延长期缺省为 4 倍标准期', () => {
  assert.deepEqual(retentionOptionsFromRuntime({ outputRetentionDays: 14 }), { retentionDays: 14, unsyncedRetentionDays: 56 })
  assert.deepEqual(
    retentionOptionsFromRuntime({ outputRetentionDays: '7', outputUnsyncedRetentionDays: '30' }),
    { retentionDays: 7, unsyncedRetentionDays: 30 },
  )
})
