// GET /api/tasks/:id/log（doc/02 §3.2）：下载 run.log，Nitro 直读共享目录
// （doc/01 §6：与 Runner 同机共享 colab_output/，不经 HTTP 中转）。
import { createReadStream, promises as fs } from 'node:fs'
import { getRouterParam, sendStream, setResponseHeaders } from 'h3'
import { findTask } from '../../../services/taskEvents'
import { taskOutputRoot, resolveTaskFilePath } from '../../../services/taskFiles'
import { sendApiError } from '../../../utils/errors'

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id')!
  const task = await findTask(id)
  if (!task) return sendApiError(event, 404, 'TASK_NOT_FOUND', `任务不存在: ${id}`)

  const logPath = resolveTaskFilePath(task.logPath, taskOutputRoot(), id, 'run.log')
  if (!logPath) {
    return sendApiError(event, 400, 'BAD_REQUEST', '日志路径不在共享输出目录内（检查 NUXT_RUNNER_OUTPUT_DIR）')
  }
  const stat = await fs.stat(logPath).catch(() => null)
  if (!stat?.isFile()) {
    return sendApiError(event, 404, 'LOG_NOT_FOUND', `日志文件不存在: ${logPath}`)
  }
  setResponseHeaders(event, {
    'Content-Type': 'text/plain; charset=utf-8',
    'Content-Length': String(stat.size),
    'Content-Disposition': `attachment; filename="${id}.log"`,
  })
  return sendStream(event, createReadStream(logPath))
})
