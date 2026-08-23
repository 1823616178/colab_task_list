import { test } from 'node:test'
import assert from 'node:assert/strict'
import { filterWorkflowDraftValues } from '../server/services/workflowOverrides.ts'
import type { EditableWorkflowField } from '../server/entities/WorkflowConfig.ts'

const FIELDS: EditableWorkflowField[] = [{
  key: 'prompt',
  label: '视频 Prompt',
  field: 'prompt',
  control: 'textarea',
}]

test('数据库旧草稿只向前台返回当前工作流仍声明的字符串字段', () => {
  assert.deepEqual(
    filterWorkflowDraftValues({ prompt: '保留', removed: '删除', invalid: 123 }, FIELDS),
    { prompt: '保留' },
  )
  assert.deepEqual(filterWorkflowDraftValues(null, FIELDS), {})
})
