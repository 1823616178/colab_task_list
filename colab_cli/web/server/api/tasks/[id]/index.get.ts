// GET /api/tasks/:id（doc/02 §3.2）：DB 行即真相（事件流实时回写）。
import { getRouterParam } from 'h3'
import { findTask } from '../../../services/taskEvents'
import { sendApiError } from '../../../utils/errors'
import { retentionFor, retentionOptionsFromRuntime } from '../../../services/outputRetention'

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')!
  const task = await findTask(id)
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)
  const options = retentionOptionsFromRuntime(useRuntimeConfig(event))
  // 保留期非法 = 定时清理已禁用（见 plugins/04.output-cleanup.ts）
  const cleanupEnabled = Number.isFinite(options.retentionDays) && options.retentionDays > 0
  const info = cleanupEnabled ? retentionFor(task, options) : null
  return {
    task,
    retention: info
      ? { reason: info.reason, expiresAt: info.expiresAt?.toISOString() ?? null, retentionDays: info.retentionDays }
      : { reason: 'disabled', expiresAt: null, retentionDays: null },
  }
})
