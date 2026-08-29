import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  parseWorkflowDraft,
  parseWorkflowDraftRecord,
  serializeWorkflowDraft,
  workflowDraftStorageKey,
} from '../utils/workflowDraft.ts'

test('工作流草稿按用户和工作流隔离', () => {
  assert.notEqual(
    workflowDraftStorageKey(1, 'minimax_h3'),
    workflowDraftStorageKey(2, 'minimax_h3'),
  )
  assert.notEqual(
    workflowDraftStorageKey(1, 'minimax_h3'),
    workflowDraftStorageKey(1, 'wan22'),
  )
})

test('工作流草稿保存和恢复包括空字符串', () => {
  const updatedAt = '2026-08-23T12:34:56.000Z'
  const raw = serializeWorkflowDraft({ prompt: '新的 Prompt', negativePrompt: '' }, updatedAt)
  assert.deepEqual(
    parseWorkflowDraft(raw, ['prompt', 'negativePrompt']),
    { prompt: '新的 Prompt', negativePrompt: '' },
  )
  assert.deepEqual(
    parseWorkflowDraftRecord(raw, ['prompt', 'negativePrompt']),
    { values: { prompt: '新的 Prompt', negativePrompt: '' }, updatedAt },
  )
})

test('v1 浏览器草稿仍可迁移，但没有更新时间', () => {
  assert.deepEqual(
    parseWorkflowDraftRecord(
      JSON.stringify({ version: 1, values: { prompt: '旧浏览器草稿' } }),
      ['prompt'],
    ),
    { values: { prompt: '旧浏览器草稿' }, updatedAt: null },
  )
})

test('工作流草稿忽略损坏数据、未知字段和非字符串值', () => {
  assert.deepEqual(parseWorkflowDraft('{', ['prompt']), {})
  assert.deepEqual(
    parseWorkflowDraft(JSON.stringify({ version: 99, values: { prompt: '旧版' } }), ['prompt']),
    {},
  )
  assert.deepEqual(
    parseWorkflowDraft(JSON.stringify({
      version: 1,
      values: { prompt: '保留', seed: '删除', invalid: 123 },
    }), ['prompt', 'invalid']),
    { prompt: '保留' },
  )
})
