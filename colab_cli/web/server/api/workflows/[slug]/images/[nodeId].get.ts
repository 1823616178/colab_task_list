// GET /api/workflows/:slug/images/:nodeId：读取工作流声明的当前输入图片。
// 全局 auth 中间件负责鉴权；路径解析只允许 input_fields 白名单与配置目录内文件。
import { createReadStream, promises as fs } from 'node:fs'
import { getRouterParam, sendStream, setResponseHeaders } from 'h3'
import { resolveWorkflowInputImage } from '../../../../services/workflows'
import { sendApiError } from '../../../../utils/errors'

const SLUG_PATTERN = /^[a-zA-Z0-9_.-]{1,128}$/
const NODE_ID_PATTERN = /^[a-zA-Z0-9_.:-]{1,128}$/

export default defineEventHandler(async (event) => {
  const slug = getRouterParam(event, 'slug') ?? ''
  const nodeId = getRouterParam(event, 'nodeId') ?? ''
  if (!SLUG_PATTERN.test(slug) || !NODE_ID_PATTERN.test(nodeId)) {
    return sendApiError(event, 400, 'BAD_REQUEST', '工作流或图片节点标识非法')
  }

  const image = await resolveWorkflowInputImage(slug, nodeId)
  if (!image) {
    return sendApiError(event, 404, 'INPUT_IMAGE_NOT_FOUND', '工作流输入图片不存在或未声明')
  }
  const stat = await fs.stat(image.path).catch(() => null)
  if (!stat?.isFile()) {
    return sendApiError(event, 404, 'INPUT_IMAGE_NOT_FOUND', '工作流输入图片不存在')
  }
  setResponseHeaders(event, {
    'Content-Type': image.contentType,
    'Content-Length': String(stat.size),
    'Cache-Control': 'no-store',
  })
  return sendStream(event, createReadStream(image.path))
})
