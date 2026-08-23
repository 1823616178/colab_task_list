import { test } from 'node:test'
import assert from 'node:assert/strict'
import { formatDurationMs } from '../utils/format.ts'

test('空耗时显示占位而不是误报 0 秒', () => {
  assert.equal(formatDurationMs(null), '—')
  assert.equal(formatDurationMs(undefined), '—')
  assert.equal(formatDurationMs(''), '—')
})

test('真实的零耗时仍显示 0 秒', () => {
  assert.equal(formatDurationMs(0), '0 秒')
})
