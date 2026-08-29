// PM2 进程配置（doc/08-PM2部署.md）。两个常驻进程：
//
//   colab-console  Nuxt 生产服务  0.0.0.0:54471  ← 浏览器
//   colab-runner   Python 作业服务 127.0.0.1:8765 ← 只有 console 打得到
//
// 用法：
//   cd colab_cli/web && npm ci && npm run build      # console 必须先构建
//   cd colab_cli && pm2 start ecosystem.config.cjs
//
// ⚠️ 用 .cjs 后缀：本仓库 web/package.json 是 "type": "module"，
//    但 PM2 加载配置走 require()。放在 colab_cli/ 下且用 .cjs 双保险。
const { resolve } = require('node:path')

const root = __dirname
const web = resolve(root, 'web')
const logs = resolve(root, 'logs')

/** 两个进程共用的日志与重启策略 */
const common = {
  cwd: web,
  // ⚠️ 绝不能开 watch：colab_output/ 就在仓库里，产物一落盘就会触发重启，
  // 而重启 runner = 正在跑的任务当场失联（见下方 colab-runner 的说明）。
  watch: false,
  // 时间戳前缀，否则 PM2 原样透传 stdout，事后无从对齐事件时间
  log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
  merge_logs: true,
  autorestart: true,
  // 崩溃重启退避：连续 10 次拉不起来就停手，别把日志刷爆
  max_restarts: 10,
  restart_delay: 3000,
  min_uptime: '30s',
}

module.exports = {
  apps: [
    {
      ...common,
      name: 'colab-console',
      script: '.output/server/index.mjs',
      // Nitro 入口自己不读 .env，与 package.json 的 `npm start` 保持一致
      node_args: '--env-file-if-exists=.env',

      // ⚠️ 必须 fork + 单实例，不能上 cluster：
      //   1) SSE 订阅管理器把「每任务一条 Nitro→Runner 长连接」存在进程内存里
      //      （server/services/taskSubscription.ts），多实例会重复订阅、
      //      重复回写 tasks 行；
      //   2) 启动补拉 plugins/03.task-recovery.ts 会被每个实例各跑一遍。
      // 要扛并发请在前面加 Nginx，不要加 console 实例。
      exec_mode: 'fork',
      instances: 1,

      max_memory_restart: '1G',
      kill_timeout: 10000,
      out_file: resolve(logs, 'console-out.log'),
      error_file: resolve(logs, 'console-error.log'),
    },
    {
      ...common,
      name: 'colab-runner',
      // 走 scripts/start-runner.mjs 而不是直接 python3 -m colab_runner：
      // 该脚本已经把 web/.env 的 NUXT_RUNNER_TOKEN → COLAB_RUNNER_TOKEN、
      // NUXT_RUNNER_OUTPUT_DIR → COLAB_OUTPUT_DIR 映射好了，密钥只有 .env 一处真相。
      // 代价是多一个 node 壳进程（~40MB），换来的是 dev（npm run runner）与
      // 生产（PM2）走同一条环境变量代码路径，不会各自漂移。
      // 壳进程已做信号转发，PM2 stop 能真正停到 python。
      script: 'scripts/start-runner.mjs',
      exec_mode: 'fork',
      instances: 1,

      // ⚠️ 任务可能跑几小时。重启 runner = 在途任务失联 → 被标 orphaned，
      // 而那台 Colab VM 还在计费。console 有启动补拉能扛自己的重启，
      // runner 没有对等机制。别顺手 `pm2 restart all`，先看 /tasks 有没有在跑的。
      // kill_timeout 给足 30s，让它有机会把手里的 VM 收干净。
      kill_timeout: 30000,
      out_file: resolve(logs, 'runner-out.log'),
      error_file: resolve(logs, 'runner-error.log'),
    },
  ],
}
