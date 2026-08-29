import type { WorkflowConfig } from '../entities/WorkflowConfig'
import { WorkflowConfigSchema } from '../entities/WorkflowConfig'
import { getDataSource } from '../utils/db'

export const WORKFLOW_DRAFT_SLUG_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/

export async function findDraftWorkflow(slug: string): Promise<WorkflowConfig | null> {
  return await (await getDataSource()).getRepository(WorkflowConfigSchema).findOne({ where: { slug } })
}
