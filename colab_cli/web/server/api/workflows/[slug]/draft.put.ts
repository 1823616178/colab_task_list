import { getRouterParam } from 'h3'
import { WorkflowDraftSchema } from '../../../entities/WorkflowDraft'
import { requireRole } from '../../../utils/auth'
import { getDataSource } from '../../../utils/db'
import { sendApiError } from '../../../utils/errors'
import { isApiError, readOptionalJsonBody } from '../../../utils/runner'
import {
  findDraftWorkflow,
  WORKFLOW_DRAFT_SLUG_PATTERN,
} from '../../../services/workflowDrafts'
import {
  normalizeWorkflowOverrides,
  WorkflowOverrideValidationError,
} from '../../../services/workflowOverrides'

// PUT /api/workflows/:slug/draft { values }：覆盖当前用户在该工作流下的草稿。
export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied

  const slug = getRouterParam(event, 'slug') ?? ''
  if (!WORKFLOW_DRAFT_SLUG_PATTERN.test(slug)) {
    return sendApiError(event, 400, 'BAD_REQUEST', `工作流 slug 非法: ${slug}`)
  }
  const workflow = await findDraftWorkflow(slug)
  if (!workflow) {
    return sendApiError(event, 404, 'NOT_FOUND', `工作流不存在: ${slug}`)
  }

  const body = await readOptionalJsonBody(event)
  if (isApiError(body)) return body
  let values: Record<string, string>
  try {
    values = normalizeWorkflowOverrides(body.values, workflow.editableFields)
  } catch (error) {
    if (error instanceof WorkflowOverrideValidationError) {
      return sendApiError(event, 400, 'BAD_REQUEST', error.message)
    }
    throw error
  }

  const repo = (await getDataSource()).getRepository(WorkflowDraftSchema)
  await repo.upsert({
    userId: event.context.auth!.userId,
    workflowConfigId: workflow.id,
    values,
  }, {
    conflictPaths: ['userId', 'workflowConfigId'],
  })
  const saved = await repo.findOneOrFail({
    where: {
      userId: event.context.auth!.userId,
      workflowConfigId: workflow.id,
    },
  })
  return { values: saved.values, updatedAt: saved.updatedAt }
})
