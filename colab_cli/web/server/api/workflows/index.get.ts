// GET /api/workflows（doc/02 §3.2）：列 workflow_configs 表（文件镜像，sync 维护）。
import { WorkflowConfigSchema } from '../../entities/WorkflowConfig'
import { getDataSource } from '../../utils/db'
import { hydrateEditableFieldDefaults } from '../../services/workflows'

export default defineEventHandler(async () => {
  const ds = await getDataSource()
  const items = await ds.getRepository(WorkflowConfigSchema).find({ order: { slug: 'ASC' } })
  return { items: await Promise.all(items.map(hydrateEditableFieldDefaults)) }
})
