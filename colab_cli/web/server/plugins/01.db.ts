import { getDataSource } from '../utils/db'

// Nitro 启动即建连；默认执行迁移，可通过 NUXT_DB_MIGRATIONS_RUN 关闭。
export default defineNitroPlugin(async () => {
  await getDataSource()
})
