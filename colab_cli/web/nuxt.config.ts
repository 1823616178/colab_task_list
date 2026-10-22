// colab-console：Nuxt 3 全栈骨架（P2：认证与基础设施）
// 见 doc/01-架构设计.md §2、doc/03-数据模型.md §1
export default defineNuxtConfig({
  compatibilityDate: '2025-08-01',
  devtools: { enabled: false },
  modules: ['@pinia/nuxt'],
  // 监听 0.0.0.0:54471（局域网可访问）。生产 .output 不读 nuxt.config，
  // 走 NITRO_HOST / NITRO_PORT 环境变量，见 package.json 的 start 脚本。
  devServer: { host: '0.0.0.0', port: 54471 },
  css: ['~/assets/css/main.css'],
  runtimeConfig: {
    // 值来自 web/.env（nuxt dev/prepare 自动加载同名 NUXT_*；生产需显式 export）
    dbHost: process.env.NUXT_DB_HOST || '127.0.0.1',
    dbPort: Number(process.env.NUXT_DB_PORT) || 3306,
    dbUser: process.env.NUXT_DB_USER || '',
    dbPassword: process.env.NUXT_DB_PASSWORD || '',
    dbName: process.env.NUXT_DB_NAME || 'colab_task',
    // 可通过 NUXT_DB_SYNCHRONIZE / NUXT_DB_MIGRATIONS_RUN 在运行时覆盖。
    dbSynchronize: false,
    dbMigrationsRun: true,
    dbLogging: false,
    jwtSecret: process.env.NUXT_JWT_SECRET || '',
    // 007_seed_admin_user 迁移使用的初始管理员账号（仅首次建库时生效）。
    // 缺省留空：未配置时迁移会跳过 seed 而不是种出一个人尽皆知的口令。
    adminUsername: process.env.NUXT_ADMIN_USERNAME || '',
    adminPassword: process.env.NUXT_ADMIN_PASSWORD || '',
    // 生产（HTTPS）必须保持 true；本地 http 调试置 false（Secure Cookie 见 README）
    authCookieSecure: process.env.NUXT_AUTH_COOKIE_SECURE !== 'false',
    // P3：Runner（127.0.0.1:8765，X-Internal-Token 共享密钥，与浏览器 JWT 独立）
    runnerUrl: process.env.NUXT_RUNNER_URL || 'http://127.0.0.1:8765',
    // 缺省留空：未配置时 runnerClient 抛 RUNNER_UNAVAILABLE，不会带着
    // 一个进过版本库的共享密钥上生产（值放 web/.env，见 .env.example）
    runnerToken: process.env.NUXT_RUNNER_TOKEN || '',
    // 仓库根（workflows/*.json 同步用）；缺省回退 <cwd>/..（dev 下 cwd=web/）
    repoRoot: '',
    // Runner 输出目录（COLAB_OUTPUT_DIR），与 Runner 同机共享：
    // log/output 由 Nitro 直读 sendStream，不经 Runner 中转（doc/01 §6）
    runnerOutputDir: '',
    // P5：终态任务产物保留天数（每日定时清理 <runnerOutputDir>/<task_id>，doc/05 P5）
    outputRetentionDays: 14,
    // 成功产物「既未下载、FTP 也未镜像成功」时的延长保留期（天）；缺省 = 4 × 标准保留期
    outputUnsyncedRetentionDays: 0,
    // 成功产物异步镜像到 FTP。全部为服务端私有配置，绝不暴露到 runtimeConfig.public。
    ftpHost: '',
    ftpPort: 21,
    ftpUser: '',
    ftpPassword: '',
    ftpDirectory: 'colab',
    ftpSecure: false,
    // 任务终态通知 Webhook（钉钉/飞书/企业微信/通用 JSON），全部服务端私有
    notifyWebhookUrl: '',
    notifyWebhookKind: '',
    notifyWebhookSecret: '',
    notifyOn: '',
    notifyConsoleUrl: '',
  },
  typescript: { strict: true },
})
