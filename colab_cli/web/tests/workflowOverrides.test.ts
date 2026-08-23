import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  normalizeWorkflowOverrides,
  WorkflowOverrideValidationError,
} from '../server/services/workflowOverrides.ts'
import type { EditableWorkflowField } from '../server/entities/WorkflowConfig.ts'

const FIELDS: EditableWorkflowField[] = [{
  key: 'prompt',
  label: '视频 Prompt',
  class_type: 'MiniMaxH3ImageToVideo',
  field: 'prompt',
  control: 'textarea',
}, {
  key: 'duration',
  label: '视频时长',
  node_id: '105:111',
  field: 'value',
  control: 'select',
  value_type: 'number',
  options: [
    { label: '5 秒', value: '5' },
    { label: '10 秒', value: '10' },
    { label: '15 秒', value: '15' },
  ],
}]

test('工作流覆盖只接受声明字段，并保留空字符串', () => {
  assert.deepEqual(normalizeWorkflowOverrides(undefined, FIELDS), {})
  assert.deepEqual(normalizeWorkflowOverrides({ prompt: '' }, FIELDS), { prompt: '' })
  assert.deepEqual(
    normalizeWorkflowOverrides({ prompt: '新的 prompt' }, FIELDS),
    { prompt: '新的 prompt' },
  )
  assert.deepEqual(normalizeWorkflowOverrides({ duration: '10' }, FIELDS), { duration: '10' })
})

test('工作流覆盖拒绝未知字段和非字符串值', () => {
  assert.throws(
    () => normalizeWorkflowOverrides({ seed: '1' }, FIELDS),
    (error: unknown) => error instanceof WorkflowOverrideValidationError
      && error.message.includes('不支持参数覆盖: seed'),
  )
  assert.throws(
    () => normalizeWorkflowOverrides({ prompt: 123 }, FIELDS),
    (error: unknown) => error instanceof WorkflowOverrideValidationError
      && error.message.includes('必须是字符串'),
  )
  assert.throws(
    () => normalizeWorkflowOverrides({ duration: '12' }, FIELDS),
    (error: unknown) => error instanceof WorkflowOverrideValidationError
      && error.message.includes('不在允许的选项中'),
  )
  assert.throws(
    () => normalizeWorkflowOverrides({ duration: 'NaN' }, FIELDS),
    (error: unknown) => error instanceof WorkflowOverrideValidationError
      && error.message.includes('必须是有效数字'),
  )
})
