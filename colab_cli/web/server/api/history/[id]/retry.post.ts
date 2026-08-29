// POST /api/history/:id/retry：失败产物手动重新加入 FTP 队列。
import { getRouterParam, setResponseStatus } from 'h3'
import { TaskSchema } from '../../../entities/Task'
import { scheduleTaskFtpUpload } from '../../../services/ftpUpload'
import { requireRole } from '../../../utils/auth'
import { getDataSource } from '../../../utils/db'
import { sendApiError } from '../../../utils/errors'
import { writeAudit } from '../../../utils/audit'

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  const id = getRouterParam(event, 'id') ?? ''
  const task = await (await getDataSource()).getRepository(TaskSchema).findOne({ where: { id } })
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)
  if (task.status !== 'succeeded' || !task.outputZipPath || task.transportMode === 'dry_run') {
    return sendApiError(event, 409, 'FTP_UPLOAD_NOT_ALLOWED', '该任务没有可上传的真实成功产物')
  }
  const queued = await scheduleTaskFtpUpload(id, true)
  if (!queued) return sendApiError(event, 409, 'FTP_UPLOAD_NOT_QUEUED', 'FTP 未配置或任务无法入队')
  await writeAudit(event, { action: 'output.ftp.retry', target: id })
  setResponseStatus(event, 202)
  return { task_id: id, ftp_status: 'pending' }
})
