import { getRouterParam } from 'h3'
import { WorkflowDraftSchema } from '../../../entities/WorkflowDraft'
import { requireRole } from '../../../utils/auth'
import { getDataSource } from '../../../utils/db'
import { sendApiError } from '../../../utils/errors'
import {
  findDraftWorkflow,
  WORKFLOW_DRAFT_SLUG_PATTERN,
} from '../../../services/workflowDrafts'
import { filterWorkflowDraftValues } from '../../../services/workflowOverrides'

// GET /api/workflows/:slug/draft：只返回当前登录用户自己的草稿。
export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator', 'viewer'])
  if (denied) return denied

  const slug = getRouterParam(event, 'slug') ?? ''
  if (!WORKFLOW_DRAFT_SLUG_PATTERN.test(slug)) {
    return sendApiError(event, 400, 'BAD_REQUEST', `工作流 slug 非法: ${slug}`)
  }
  const workflow = await findDraftWorkflow(slug)
  if (!workflow) {
    return sendApiError(event, 404, 'NOT_FOUND', `工作流不存在: ${slug}`)
  }

  const row = await (await getDataSource()).getRepository(WorkflowDraftSchema).findOne({
    where: {
      userId: event.context.auth!.userId,
      workflowConfigId: workflow.id,
    },
  })
  return {
    draft: row
      ? {
          values: filterWorkflowDraftValues(row.values, workflow.editableFields),
          updatedAt: row.updatedAt,
        }
      : null,
  }
})
