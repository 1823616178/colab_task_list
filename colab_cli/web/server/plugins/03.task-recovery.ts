// P3 启动收尾（doc/05 P3）：
// 1. Runner 健康检查（doc/02 §2.1）：不可达 → 告警（不阻断启动，提交任务时才 503）；
//    transport probe 失败 → Runner 已自动降级 cli，显式告警（doc/01 §5 R1）。
// 2. 启动补拉：对 DB 里所有非终态（queued/running）任务建立 SSE 订阅，
//    from_seq = tasks.lastSeq —— 覆盖 Nitro 宕机窗口，否则任务永远停在 running。
import { In } from 'typeorm'
import { TaskSchema } from '../entities/Task'
import { getDataSource } from '../utils/db'
import { checkRunnerHealth, runnerRequest } from '../services/runnerClient'
import { ensureTaskSubscription } from '../services/taskSubscription'
import { finalizeTaskFromStoredTerminalEvent } from '../services/taskEvents'

const RUNNER_TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'canceled'])

export default defineNitroPlugin(async () => {
  let runnerAvailable = false
  try {
    const health = await checkRunnerHealth()
    runnerAvailable = true
    if (health.transport?.degraded) {
      console.warn(
        `[runner] transport 已降级为 ${health.transport.mode}`
          + `（私有 API 探测失败: ${JSON.stringify(health.transport.probe)}）`,
      )
    }
  } catch (error) {
    console.warn('[runner] 健康检查失败（Runner 未启动？），提交任务将返回 503:', error instanceof Error ? error.message : error)
  }

  const ds = await getDataSource()
  const inflight = await ds.getRepository(TaskSchema).find({
    where: { status: In(['queued', 'running']) },
    select: { id: true, lastSeq: true },
  })
  for (const task of inflight) {
    if (runnerAvailable) {
      try {
        const snapshot = await runnerRequest<{ status: string }>(`/tasks/${task.id}`)
        if (RUNNER_TERMINAL_STATUSES.has(snapshot.status)) {
          const repaired = await finalizeTaskFromStoredTerminalEvent(task.id, task.lastSeq)
          if (repaired) {
            console.warn(
              `[task-sub] 已修复终态未收尾任务: ${task.id}`
                + `（Runner=${snapshot.status}, lastSeq=${task.lastSeq}）`,
            )
            continue
          }
        }
      } catch (error) {
        console.warn(
          `[task-sub] 启动核对 ${task.id} 状态失败，将回退 SSE 补拉:`,
          error instanceof Error ? error.message : error,
        )
      }
    }
    console.log(`[task-sub] 启动补拉: ${task.id} from_seq=${task.lastSeq}`)
    ensureTaskSubscription(task.id, task.lastSeq)
  }
})
