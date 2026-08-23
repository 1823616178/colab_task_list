// POST /api/workflows/sync（operator+，doc/02 §3.2）：workflows/*.json → 表。
import { requireRole } from '../../utils/auth'
import { sendApiError } from '../../utils/errors'
import { writeAudit } from '../../utils/audit'
import { syncWorkflowConfigs } from '../../services/workflows'

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  try {
    const items = await syncWorkflowConfigs()
    await writeAudit(event, {
      action: 'workflow.sync',
      detail: { synced: items.length, slugs: items.map(item => item.slug) },
    })
    return { synced: items.length, slugs: items.map(item => item.slug) }
  } catch (error) {
    return sendApiError(event, 500, 'WORKFLOW_SYNC_FAILED',
      error instanceof Error ? error.message : String(error))
  }
})
