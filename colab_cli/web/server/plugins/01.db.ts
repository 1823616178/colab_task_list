import { getDataSource } from '../utils/db'

// Nitro 启动即建连并跑 migration（migrationsRun: true，含 001 建表与 007 admin seed）
export default defineNitroPlugin(async () => {
  await getDataSource()
})
