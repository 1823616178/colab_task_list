// 任务终态 → Webhook 通知（读 DB 行组装摘要；失败只告警）。
import { TaskSchema } from '../entities/Task'
import { getDataSource } from '../utils/db'
import {
  buildWebhookRequest,
  notifyConfigFromRuntime,
  postWebhook,
  shouldNotify,
} from './notify'

export async function notifyTaskFinished(taskId: string): Promise<void> {
  try {
    const config = notifyConfigFromRuntime(useRuntimeConfig() as unknown as Record<string, unknown>)
    if (!config) return
    const task = await (await getDataSource()).getRepository(TaskSchema).findOne({ where: { id: taskId } })
    if (!task || !shouldNotify(config, task.status)) return
    await postWebhook(buildWebhookRequest(config, task))
  } catch (error) {
    console.warn(`[notify] ${taskId} 通知发送失败:`, error instanceof Error ? error.message : error)
  }
}
