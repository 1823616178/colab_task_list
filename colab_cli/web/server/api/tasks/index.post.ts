// POST /api/tasks（operator+，doc/02 §2.3/§3.2）
// 流程：校验 body → Runner 健康检查 → workflow sync-on-demand（避免 FK 失败）→
// 加速器兼容性前置校验 → 转发 Runner POST /tasks → 用 Runner 返回的 task_id 建
// Task 行（status=queued，快照路径按 doc/02 §2.3.1 布局约定）→ 启动该任务的
// SSE 订阅（生命周期驱动，见 services/taskSubscription.ts）。
import path from 'node:path'
import { setResponseStatus } from 'h3'
import { QueryFailedError } from 'typeorm'
import { TaskSchema } from '../../entities/Task'
import { SessionSchema } from '../../entities/Session'
import { requireRole } from '../../utils/auth'
import { getDataSource } from '../../utils/db'
import { sendApiError } from '../../utils/errors'
import { isApiError, readOptionalJsonBody, sendRunnerError } from '../../utils/runner'
import { checkRunnerHealth, runnerRequest } from '../../services/runnerClient'
import {
  ensureWorkflowConfig,
  hydrateEditableFieldDefaults,
  repoRoot,
} from '../../services/workflows'
import { ensureTaskSubscription } from '../../services/taskSubscription'
import { writeAudit } from '../../utils/audit'
import {
  normalizeWorkflowOverrides,
  WorkflowOverrideValidationError,
} from '../../services/workflowOverrides'
import { findLiveSessionConflict } from '../../services/sessionReconcileRules'
import type { RunnerSessionSnapshot } from '../../services/sessionReconcileRules'

import {
  BACKEND_GPU_SPECS,
  BACKEND_TPU_SPECS,
  COMPUTE_BACKENDS,
  isComputeBackend,
} from '../../../utils/computeBackends'

const SESSION_NAME_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/
const WORKFLOW_SLUG_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/

function runnerOutputDir(): string {
  const config = useRuntimeConfig()
  return path.resolve(String(config.runnerOutputDir || '') || path.join(repoRoot(), 'colab_output'))
}

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied

  const body = await readOptionalJsonBody(event)
  if (isApiError(body)) return body

  // ── body 校验（doc/02 §2.3 字段 + dry_run 透传）────────────────
  const workflowConfig = body.workflow_config
  if (typeof workflowConfig !== 'string' || !workflowConfig) {
    return sendApiError(event, 400, 'BAD_REQUEST', '缺少 workflow_config（如 workflows/minimax_h3.json）')
  }
  const slug = workflowConfig.replace(/^workflows\//, '').replace(/\.json$/i, '')
  if (!WORKFLOW_SLUG_PATTERN.test(slug)) {
    return sendApiError(event, 400, 'BAD_REQUEST', `workflow_config 非法: ${workflowConfig}`)
  }
  const accelerator = (body.accelerator ?? {}) as Record<string, unknown>
  const kind = typeof accelerator.kind === 'string' ? accelerator.kind : 'gpu'
  const spec = typeof accelerator.spec === 'string' ? accelerator.spec : 'T4'
  if (kind !== 'gpu' && kind !== 'tpu') {
    return sendApiError(event, 400, 'ACCELERATOR_UNSUPPORTED', `未知加速器类型: ${kind}`)
  }
  const backend = body.backend === undefined || body.backend === null ? 'colab' : body.backend
  if (!isComputeBackend(backend)) {
    return sendApiError(event, 400, 'BAD_REQUEST', `未知计算后端: ${String(backend)}，可选: ${COMPUTE_BACKENDS.join(', ')}`)
  }
  const validSpecs = (kind === 'gpu' ? BACKEND_GPU_SPECS : BACKEND_TPU_SPECS)[backend]
  if (!validSpecs.includes(spec)) {
    return sendApiError(event, 400, 'ACCELERATOR_UNSUPPORTED',
      `${backend} 后端的 ${kind} 不支持规格 ${spec}，可选: ${validSpecs.join(', ') || '无'}`)
  }
  const session = typeof body.session === 'string' && body.session ? body.session : 'comfyui-h3'
  if (!SESSION_NAME_PATTERN.test(session)) {
    return sendApiError(event, 400, 'BAD_REQUEST', `session 名非法: ${session}`)
  }
  const timeout = body.timeout === undefined ? 86400 : Number(body.timeout)
  if (!Number.isFinite(timeout) || timeout <= 0) {
    return sendApiError(event, 400, 'BAD_REQUEST', 'timeout 必须是正数（秒）')
  }
  for (const key of ['mount_drive', 'reuse', 'keep', 'dry_run'] as const) {
    if (body[key] !== undefined && typeof body[key] !== 'boolean') {
      return sendApiError(event, 400, 'BAD_REQUEST', `${key} 必须是布尔值`)
    }
  }
  // 上面的循环已保证这四个字段要么是 boolean 要么缺省。这里落成具名常量：
  // 既让 TS 看到 boolean（`unknown ?? true` 会退化成 `{}`，插库时报 TS2322），
  // 也避免「转发 Runner」与「入库」两处各写一遍默认值而漂移。
  const mountDrive = body.mount_drive === undefined ? true : body.mount_drive === true
  const reuse = body.reuse === true
  const keep = body.keep === true
  const dryRun = body.dry_run === true

  for (const key of ['api_json', 'input_dir'] as const) {
    if (body[key] !== undefined && body[key] !== null && typeof body[key] !== 'string') {
      return sendApiError(event, 400, 'BAD_REQUEST', `${key} 必须是字符串或 null`)
    }
  }

  // ── Runner 健康检查（失败 → 503 RUNNER_UNAVAILABLE）──────────────
  try {
    await checkRunnerHealth()
  } catch (error) {
    return sendRunnerError(event, error)
  }

  const ds = await getDataSource()
  // 新建模式不能覆盖同名映射：colab CLI 覆盖本地 name→endpoint 后，旧 VM 会
  // 变成名称为 "?" 的计费孤儿。DB endpoint 可在名称丢失时兜底识别旧 VM。
  // Colab 才有「同名映射覆盖成孤儿 VM」问题；Kaggle/Modal 的会话由各自后端管理
  if (!dryRun && !reuse && backend === 'colab') {
    let runnerSessions: RunnerSessionSnapshot[]
    try {
      runnerSessions = await runnerRequest<RunnerSessionSnapshot[]>('/sessions')
    } catch (error) {
      return sendRunnerError(event, error)
    }
    const stored = await ds.getRepository(SessionSchema).findOne({
      where: { name: session },
      select: { endpoint: true },
    })
    const conflict = findLiveSessionConflict(session, runnerSessions, stored?.endpoint)
    if (conflict) {
      return sendApiError(
        event,
        409,
        'SESSION_ALREADY_EXISTS',
        `会话 ${session} 已有存活 VM；请选择复用，或先在会话页停止旧 VM`,
        { session, endpoint: conflict.endpoint ?? null },
      )
    }
  }

  // ── workflow sync-on-demand + 加速器兼容性前置校验 ──────────────
  const workflow = await ensureWorkflowConfig(slug)
  if (!workflow) {
    return sendApiError(event, 400, 'BAD_REQUEST', `工作流配置不存在: workflows/${slug}.json`)
  }
  if (workflow.supportedAccelerators.length > 0 && !workflow.supportedAccelerators.includes(kind)) {
    return sendApiError(event, 400, 'ACCELERATOR_UNSUPPORTED',
      workflow.acceleratorNote ?? `工作流 ${workflow.name} 不支持 ${kind}`,
      { slug, supported: workflow.supportedAccelerators })
  }
  let workflowOverrides: Record<string, string>
  try {
    const workflowView = await hydrateEditableFieldDefaults(workflow)
    workflowOverrides = normalizeWorkflowOverrides(
      body.workflow_overrides,
      workflowView.editableFields,
    )
  } catch (error) {
    if (error instanceof WorkflowOverrideValidationError) {
      return sendApiError(event, 400, 'BAD_REQUEST', error.message)
    }
    throw error
  }

  // ── 转发 Runner（POST 不重试，见 services/runnerClient.ts）───────
  let runnerResult: { task_id: string, status: string }
  try {
    runnerResult = await runnerRequest<{ task_id: string, status: string }>('/tasks', {
      method: 'POST',
      body: {
        workflow_config: `workflows/${slug}.json`,
        api_json: body.api_json ?? null,
        session,
        accelerator: { kind, spec },
        backend,
        mount_drive: mountDrive,
        reuse,
        keep,
        input_dir: body.input_dir ?? null,
        workflow_overrides: workflowOverrides,
        timeout,
        ...(dryRun ? { dry_run: true } : {}),
      },
    })
  } catch (error) {
    return sendRunnerError(event, error) // SESSION_BUSY 409 等原样透传
  }

  // ── 建 Task 行（主键直接用 Runner 生成的 task_id，保证一致性）─────
  const taskId = runnerResult.task_id
  const outDir = path.join(runnerOutputDir(), taskId)
  try {
    await ds.getRepository(TaskSchema).insert({
      id: taskId,
      workflowConfigId: workflow.id,
      workflowName: workflow.name,
      sessionName: session,
      backend,
      acceleratorKind: kind,
      acceleratorSpec: spec,
      mountDrive,
      reuse,
      keep,
      timeout,
      status: 'queued',
      apiJsonSnapshotPath: path.join(outDir, 'api.json'),
      workflowSnapshotPath: path.join(outDir, 'workflow.json'),
      eventsPath: path.join(outDir, 'events.jsonl'),
      logPath: path.join(outDir, 'run.log'),
      createdByUserId: event.context.auth!.userId,
    })
  } catch (error) {
    // 建库失败（任何原因）都要尽力回收 Runner 侧已受理的任务，
    // 否则留下「Runner 在跑、DB 无行、无订阅」的孤儿任务
    await runnerRequest(`/tasks/${taskId}/cancel`, { method: 'POST', body: { mode: 'interrupt' } }).catch(() => {})
    // activeSessionKey 唯一索引兜底 session 互斥（Runner 409 之后的第二道防线）：
    // 撞索引说明库中已有同 session 非终态任务，报 409
    if (error instanceof QueryFailedError
      && String((error as { driverError?: { code?: string } }).driverError?.code) === 'ER_DUP_ENTRY') {
      return sendApiError(event, 409, 'SESSION_BUSY', `会话 ${session} 已有非终态任务（数据库约束）`, { session })
    }
    throw error
  }

  // ── 启动 SSE 订阅（from_seq=-1：Runner 会回放 task.queued 起的全部事件）──
  ensureTaskSubscription(taskId, -1)

  await writeAudit(event, {
    action: 'task.submit',
    target: taskId,
    detail: {
      workflow: workflow.slug,
      session,
      accelerator: { kind, spec },
      backend,
      reuse,
      keep,
      dry_run: dryRun,
      workflow_override_keys: Object.keys(workflowOverrides),
    },
  })

  setResponseStatus(event, 202)
  return { task_id: taskId, status: 'queued' }
})
