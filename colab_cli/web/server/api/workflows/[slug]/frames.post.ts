// POST /api/workflows/:slug/frames（operator+，doc/02 §3.2）：首/尾帧上传，
// 替代手工跑 set_frames.py。multipart 接收 → 落临时文件 → 转发 Runner
// POST /workflows/{slug}/frames（Runner 与本机同机，按绝对路径读取，doc/02 §2.2）。
// ⚠️ 尾帧（last）是否可用取决于目标工作流 MiniMaxH3ImageToVideo 是否接了
// last_frame。仓库根 api.json（minimax_h3）已接节点 122；未接线时 Runner
// 明确拒绝（400 FRAMES_UPDATE_FAILED）—— 错误原样透传，这里不做预判。
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { getRouterParam, readMultipartFormData } from 'h3'
import { requireRole } from '../../../utils/auth'
import { sendApiError } from '../../../utils/errors'
import { sendRunnerError } from '../../../utils/runner'
import { writeAudit } from '../../../utils/audit'
import { runnerRequest } from '../../../services/runnerClient'

const SLUG_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/
// 与 set_frames.py 的 IMAGE_EXTENSIONS 对齐（Runner 侧仍是最终权威）
const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff'])
const MAX_FRAME_BYTES = 20 * 1024 * 1024 // 帧图片 20 MiB 上限

interface FrameUpload {
  field: 'first' | 'last'
  filename: string
  data: Buffer
}

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  const slug = getRouterParam(event, 'slug') ?? ''
  if (!SLUG_PATTERN.test(slug)) {
    return sendApiError(event, 400, 'BAD_REQUEST', `工作流 slug 非法: ${slug}`)
  }

  const parts = await readMultipartFormData(event).catch(() => null)
  if (!parts) {
    return sendApiError(event, 400, 'BAD_REQUEST', '请求体必须是 multipart/form-data')
  }

  const frames: FrameUpload[] = []
  for (const part of parts) {
    if (part.name !== 'first' && part.name !== 'last') continue
    if (!part.filename || !part.data?.length) continue
    const ext = path.extname(part.filename).toLowerCase()
    if (!IMAGE_EXTENSIONS.has(ext)) {
      return sendApiError(event, 400, 'BAD_REQUEST',
        `不支持的图片格式 ${ext || '（无扩展名）'}，仅支持: ${[...IMAGE_EXTENSIONS].join(', ')}`)
    }
    if (part.data.length > MAX_FRAME_BYTES) {
      return sendApiError(event, 400, 'BAD_REQUEST',
        `图片 ${part.filename} 超过 ${MAX_FRAME_BYTES / 1024 / 1024} MiB 上限`)
    }
    frames.push({ field: part.name, filename: part.filename, data: part.data })
  }
  if (frames.length === 0) {
    return sendApiError(event, 400, 'BAD_REQUEST', '至少需要提供首帧（first）或尾帧（last）之一')
  }

  // 落临时文件（按 field 分子目录，两个同名文件互不覆盖、原名保留）
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), 'colab-frames-'))
  try {
    const body: { first?: string, last?: string } = {}
    for (const frame of frames) {
      const dir = path.join(tmpDir, frame.field)
      await fs.mkdir(dir, { recursive: true })
      const target = path.join(dir, path.basename(frame.filename))
      await fs.writeFile(target, frame.data)
      body[frame.field] = target
    }

    let result: { ok: boolean, slug: string, api_json: string, output: string }
    try {
      // set_frames.py 在 Runner 内有 60s 子进程上限，这里留 70s
      result = await runnerRequest(`/workflows/${encodeURIComponent(slug)}/frames`, {
        method: 'POST',
        body,
        timeoutMs: 70_000,
      })
    } catch (error) {
      // Runner 业务错误（last_frame 未接线等）原样透传：同码同消息（doc/02 §3.4）
      return sendRunnerError(event, error)
    }

    await writeAudit(event, {
      action: 'workflow.frames.upload',
      target: slug,
      detail: {
        first: Boolean(body.first),
        last: Boolean(body.last),
        filenames: frames.map(frame => frame.filename),
      },
    })
    return result
  } finally {
    await fs.rm(tmpDir, { recursive: true, force: true }).catch(() => {})
  }
})
