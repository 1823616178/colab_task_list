// GET /api/history：只列真实通道成功且已有本地产物的历史记录。
import { getQuery } from 'h3'
import { In, Not, IsNull } from 'typeorm'
import { TaskSchema } from '../../entities/Task'
import { getDataSource } from '../../utils/db'

export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const page = Math.max(1, Number(query.page) || 1)
  const size = Math.min(100, Math.max(1, Number(query.size) || 20))
  const [items, total] = await (await getDataSource()).getRepository(TaskSchema).findAndCount({
    where: {
      status: 'succeeded',
      transportMode: In(['native', 'cli']),
      outputZipPath: Not(IsNull()),
    },
    order: { finishedAt: 'DESC' },
    skip: (page - 1) * size,
    take: size,
  })
  return { items, total, page, size }
})
