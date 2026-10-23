// GET /api/workflows（doc/02 §3.2）：列 workflow_configs 表（文件镜像，sync 维护）。
import { hydrateEditableFieldDefaults, listWorkflowConfigs } from '../../services/workflows'

export default defineEventHandler(async () => {
  const items = await listWorkflowConfigs()
  return { items: await Promise.all(items.map(hydrateEditableFieldDefaults)) }
})
