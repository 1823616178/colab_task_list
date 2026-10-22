// GET /api/tasks/:id/output（doc/02 §3.2）：sendStream 直读共享目录的 output.zip，
// **不经 Runner 中转**（包动辄几百 MB，doc/01 §6）。
import { createReadStream, promises as fs } from 'node:fs'
import { getRouterParam, sendStream, setResponseHeaders } from 'h3'
import { findTask } from '../../../services/taskEvents'
import { resolveTaskFilePath, taskOutputRoot } from '../../../services/taskFiles'
import { sendApiError } from '../../../utils/errors'
import { getDataSource } from '../../../utils/db'
import { TaskSchema } from '../../../entities/Task'

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')!
  const task = await findTask(id)
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)
  if (!task.outputZipPath) {
    return sendApiError(event, 409, 'OUTPUT_NOT_READY', `任务尚无输出产物（状态 ${task.status}）`)
  }
  const zipPath = resolveTaskFilePath(task.outputZipPath, taskOutputRoot(), id, 'output.zip')
  if (!zipPath) {
    return sendApiError(event, 400, 'BAD_REQUEST', '输出路径不在共享输出目录内（检查 NUXT_RUNNER_OUTPUT_DIR）')
  }
  const stat = await fs.stat(zipPath).catch(() => null)
  if (!stat?.isFile()) {
    return sendApiError(event, 404, 'OUTPUT_FILE_MISSING', `输出文件已不存在: ${zipPath}`)
  }
  if (!task.outputDownloadedAt) {
    // 记首次下载：产物已有用户侧副本，清理策略回落到标准保留期
    await (await getDataSource()).getRepository(TaskSchema)
      .update({ id }, { outputDownloadedAt: new Date() })
      .catch(error => console.warn(`[output] ${id} 记录下载时间失败:`, error))
  }
  setResponseHeaders(event, {
    'Content-Type': 'application/zip',
    'Content-Length': String(stat.size),
    'Content-Disposition': `attachment; filename="output-${id}.zip"`,
    ...(task.outputSha256 ? { 'X-Output-Sha256': task.outputSha256 } : {}),
  })
  return sendStream(event, createReadStream(zipPath))
})
