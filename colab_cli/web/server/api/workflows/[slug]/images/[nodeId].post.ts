import { getRouterParam, readMultipartFormData } from 'h3'
import { repoRoot } from '../../../../services/workflows'
import { updateWorkflowInputImage } from '../../../../services/workflowImageUpload'
import { requireRole } from '../../../../utils/auth'
import { sendApiError } from '../../../../utils/errors'
import { writeAudit } from '../../../../utils/audit'

const SLUG_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/
const NODE_ID_PATTERN = /^[a-zA-Z0-9_.:-]{1,128}$/
const MAX_IMAGE_BYTES = 20 * 1024 * 1024

export default defineEventHandler(async (event) => {
  const denied = requireRole(event, ['admin', 'operator'])
  if (denied) return denied
  const slug = getRouterParam(event, 'slug') ?? ''
  const nodeId = getRouterParam(event, 'nodeId') ?? ''
  if (!SLUG_PATTERN.test(slug) || !NODE_ID_PATTERN.test(nodeId)) {
    return sendApiError(event, 400, 'BAD_REQUEST', '工作流或图片节点标识非法')
  }
  const parts = await readMultipartFormData(event).catch(() => null)
  const image = parts?.find(part => part.name === 'image' && part.filename && part.data?.length)
  if (!image?.filename) {
    return sendApiError(event, 400, 'BAD_REQUEST', '请选择图片文件')
  }
  if (image.data.length > MAX_IMAGE_BYTES) {
    return sendApiError(event, 400, 'BAD_REQUEST', '图片不能超过 20 MiB')
  }
  const result = await updateWorkflowInputImage(repoRoot(), slug, nodeId, image.filename, image.data)
  if (result === 'not_found') {
    return sendApiError(event, 404, 'INPUT_IMAGE_NOT_FOUND', '工作流图片节点不存在或未声明')
  }
  if (result === 'unsupported') {
    return sendApiError(event, 400, 'BAD_REQUEST', '不支持的图片格式')
  }
  await writeAudit(event, {
    action: 'workflow.image.upload',
    target: `${slug}/${nodeId}`,
    detail: { filename: image.filename },
  })
  return { ok: true }
})
