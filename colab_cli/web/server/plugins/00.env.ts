// 在 TypeORM migration 之前把 runtimeConfig 注入 process.env：
// 007_seed_admin_user 是纯迁移类，拿不到 nuxt 上下文，只能读 env。
export default defineNitroPlugin(() => {
  const config = useRuntimeConfig()
  if (config.adminUsername && !process.env.ADMIN_USERNAME) {
    process.env.ADMIN_USERNAME = String(config.adminUsername)
  }
  if (config.adminPassword && !process.env.ADMIN_PASSWORD) {
    process.env.ADMIN_PASSWORD = String(config.adminPassword)
  }
})
