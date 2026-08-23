import { EntitySchema } from 'typeorm'

/** 用户在提交任务页保存的工作流参数草稿。 */
export interface WorkflowDraft {
  /** bigint → mysql2 返回 string */
  id: string
  userId: number
  workflowConfigId: number
  values: Record<string, string>
  createdAt: Date
  updatedAt: Date
}

export const WorkflowDraftSchema = new EntitySchema<WorkflowDraft>({
  name: 'WorkflowDraft',
  tableName: 'workflow_drafts',
  columns: {
    id: { type: 'bigint', primary: true, generated: 'increment' },
    userId: { type: 'int' },
    workflowConfigId: { type: 'int' },
    values: { type: 'json' },
    createdAt: { type: 'datetime', precision: 6, createDate: true },
    updatedAt: { type: 'datetime', precision: 6, updateDate: true },
  },
  uniques: [
    {
      name: 'UQ_workflow_drafts_user_workflow',
      columns: ['userId', 'workflowConfigId'],
    },
  ],
  indices: [
    { name: 'IDX_workflow_drafts_workflow', columns: ['workflowConfigId'] },
  ],
})
