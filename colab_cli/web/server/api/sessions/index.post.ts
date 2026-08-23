// POST /api/sessions（operator+）：启动 Colab VM（转发 Runner POST /sessions）。
// body: { name, accelerator: { kind, spec }, mount_drive? }。colab new 可能要一两分钟，
// 超时 180s。成功后写审计并对账，让 sessions 表立刻 expectedAlive。
import { setResponseStatus } from 'h3'
import { requireRole } from '../../utils/auth'
import { sendApiError } from '../../utils/errors'
import { isApiError, readOptionalJsonBody, sendRunnerError } from '../../utils/runner'
import { runnerRequest } from '../../services/runnerClient'
import { writeAudit } from '../../utils/audit'
import { runSessionReconcile } from '../../services/sessionReconcile'
import { getDataSource } from '../../utils/db'
import { SessionSchema } from '../../entities/Session'
import { findLiveSessionConflict } from '../../services/sessionReconcileRules'
import type { RunnerSessionSnapshot } from '../../services/sessionReconcileRules'

const GPU_SPECS = ['T4', 'L4', 'G4', 'H100', 'A100']
const TPU_SPECS = ['v5e1', 'v6e1']
const SESSION_NAME_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/
const CREATE_TIMEOUT_MS = 180_000

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied

  const body = await readOptionalJsonBody(event)
  if (isApiError(body)) return body

  const name = typeof body.name === 'string' ? body.name.trim()
    : (typeof body.session === 'string' ? body.session.trim() : '')
  if (!SESSION_NAME_PATTERN.test(name)) {
    return sendApiError(event, 400, 'BAD_REQUEST', '会话名仅允许字母、数字、_ . -，最长 128 字符')
  }
  const accelerator = (body.accelerator ?? {}) as Record<string, unknown>
  const kind = typeof accelerator.kind === 'string' ? accelerator.kind : 'gpu'
  const spec = typeof accelerator.spec === 'string' ? accelerator.spec : 'T4'
  if (kind !== 'gpu' && kind !== 'tpu') {
    return sendApiError(event, 400, 'ACCELERATOR_UNSUPPORTED', `未知加速器类型: ${kind}`)
  }
  const validSpecs = kind === 'gpu' ? GPU_SPECS : TPU_SPECS
  if (!validSpecs.includes(spec)) {
    return sendApiError(event, 400, 'ACCELERATOR_UNSUPPORTED', `${kind} 不支持规格 ${spec}，可选: ${validSpecs.join(', ')}`)
  }
  const mountDrive = body.mount_drive === undefined ? false : Boolean(body.mount_drive)

  let runnerSessions: RunnerSessionSnapshot[]
  try {
    runnerSessions = await runnerRequest<RunnerSessionSnapshot[]>('/sessions')
  } catch (error) {
    return sendRunnerError(event, error)
  }
  const ds = await getDataSource()
  const stored = await ds.getRepository(SessionSchema).findOne({
    where: { name },
    select: { endpoint: true },
  })
  const conflict = findLiveSessionConflict(name, runnerSessions, stored?.endpoint)
  if (conflict) {
    return sendApiError(
      event,
      409,
      'SESSION_ALREADY_EXISTS',
      `会话 ${name} 已有存活 VM，请先停止旧 VM`,
      { name, endpoint: conflict.endpoint ?? null },
    )
  }

  let result: { ok: boolean, name: string, log: string }
  try {
    result = await runnerRequest(
      '/sessions',
      {
        method: 'POST',
        body: { name, accelerator: { kind, spec }, mount_drive: mountDrive },
        timeoutMs: CREATE_TIMEOUT_MS,
      },
    )
  } catch (error) {
    return sendRunnerError(event, error)
  }

  await writeAudit(event, {
    action: 'session.create',
    target: name,
    detail: { accelerator: { kind, spec }, mount_drive: mountDrive },
  })
  await runSessionReconcile().catch((error: unknown) => {
    console.warn('[session-reconcile] 启动会话后对账失败:', error)
  })

  setResponseStatus(event, 201)
  return result
})
