import { getRouterParam } from 'h3'
import { WorkflowDraftSchema } from '../../../entities/WorkflowDraft'
import { requireRole } from '../../../utils/auth'
import { getDataSource } from '../../../utils/db'
import { sendApiError } from '../../../utils/errors'
import {
  findDraftWorkflow,
  WORKFLOW_DRAFT_SLUG_PATTERN,
} from '../../../services/workflowDrafts'

// DELETE /api/workflows/:slug/draft：恢复文件默认值时删除当前用户自己的草稿。
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

  await (await getDataSource()).getRepository(WorkflowDraftSchema).delete({
    userId: event.context.auth!.userId,
    workflowConfigId: workflow.id,
  })
  return { ok: true }
})
