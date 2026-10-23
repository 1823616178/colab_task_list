# colab-console（web/）

Colab 编排器的 Nuxt 3 后台：纯展示与管理层，**只有本服务通过 TypeORM 写 MySQL**
（铁律，见 `doc/03-数据模型.md` §1）。已完成 P2 认证、P3 任务主链路、
P4 会话对账/防孤儿 VM、P5 工作流/设帧/用户/清理/审计。

## 启动

```bash
npm install
cp .env.example .env   # 填入真实值
npm run dev            # 开发（0.0.0.0:54471；端口占用时自动顺延，见下）
npm run build          # 生产构建（.output/）
npm start              # 生产运行（0.0.0.0:54471）
```

Python Runner 在另一个终端运行 `npm run runner`，自动读取 `web/.env` 的
`NUXT_RUNNER_TOKEN` 与输出目录配置，默认监听 `127.0.0.1:8765`。
首次运行先执行 `python -m pip install -r ../requirements.txt`（Linux / macOS 用 `python3`）。
启动脚本在 Windows 默认使用当前 PATH 中的 `python`（可先激活 Conda 环境），
Linux / macOS 默认使用 `python3`；也可以用环境变量 `COLAB_PYTHON` 指定解释器路径。

当前锁定的 `google-colab-cli==0.6.0` 在原生 Windows 启动命令时会导入 Unix 专用的
`termios`，报 `ModuleNotFoundError: No module named 'termios'`。
Runner HTTP 服务能够启动，但 Colab 任务仍需在 WSL/Linux 或 Linux Docker 容器中运行；
Docker 部署方式见[上级 README](../README.md#docker-一键启动web--runner复用已有-mysql)。

Colab CLI 0.6.0 的内核依赖也需固定为 `jupyter-kernel-client==0.9.0`
（已写入 `../requirements.txt`）。PyPI 1.0.2 已不再导出 CLI 使用的 `KernelClient`，
会导致 `drivemount`、`exec` 同时报 AttributeError；详见
[上游兼容性问题](https://github.com/googlecolab/google-colab-cli/issues/137)。
Docker 构建会验证执行 API；Runner 运行时发现依赖不兼容会让 `/health` 返回 503，
避免继续创建 VM 后才失败。直接部署修复后需重启 Runner，Docker 部署需重建 Runner 镜像。

Runner 的原生执行通道与 CLI 共用 `COLAB_CLI_STATE_DIR/<session>.json`（或
`COLAB_CONFIG`）映射，读取和内核 ID 回写均使用会话自己的 StateStore，避免并发任务
切换全局配置。若在工作流提交前就确认映射缺失或内核预检失败，新建且未要求保留的
VM 会自动停止；工作流提交后的异常仍保留 VM，以保护可能已经生成的产物。

**监听地址 `0.0.0.0:54471`**（局域网可访问，不只是本机）。三个入口各读各的：

| 入口 | 来源 |
| :--- | :--- |
| `npm run dev` | `nuxt.config.ts` 的 `devServer`（端口被占用时 get-port 自动顺延到 54472…） |
| `npm run preview` | `.env` 的 `NITRO_HOST` / `NITRO_PORT` |
| `npm start` | 自动加载 `.env`，由 `NITRO_HOST` / `NITRO_PORT` 决定监听地址 |

改端口时同步修改 `devServer` 与 `.env`，否则 dev 与生产会跑在不同端口上。

Nitro 入口本身不会读取 `.env`；项目的 `npm start` 使用 Node
`--env-file-if-exists=.env` 补上这一步。容器或进程管理器已经注入的环境变量
优先级更高，因此生产部署也可以不提供该文件。

生产常驻用 PM2（`colab_cli/ecosystem.config.cjs`，把本服务与 Python Runner
一起拉起，日志落 `colab_cli/logs/`）—— 见
[doc/08-PM2部署.md](../doc/08-PM2部署.md)。⚠️ PM2 跑的是 `.output/` 里的产物，
改完代码要 `npm run build && pm2 restart colab-console`，只 restart 不 build
跑的还是旧产物。

⚠️ 绑 `0.0.0.0` 意味着同网段任何机器都能打到登录页。Python Runner
（`127.0.0.1:8765`）**保持只绑本机不变** —— 它凭 ADC 就能建 A100 VM，
绝不能上网络（doc/01 §7）。

Nitro 启动时自动连接 MySQL，默认执行 migration（含 001 建表与 007 admin seed），
无需手工迁移命令。可在 `.env` 设置 `NUXT_DB_SYNCHRONIZE=false` 与
`NUXT_DB_MIGRATIONS_RUN=true` 控制实体自动同步和启动迁移，修改后重启 Web 生效。
生产保持上述默认值；实体自动同步可能修改或删除已有表结构。

## 环境变量

密钥一律走 `web/.env`（已 gitignore，模板见 `.env.example`）。
`nuxt.config.ts` 里的 `runtimeConfig` **只写空串缺省**，不写真实值 ——
它是要进版本库的文件。缺省为空时的行为：`NUXT_RUNNER_TOKEN` 缺失 →
`runnerClient` 抛 `RUNNER_UNAVAILABLE`；`NUXT_ADMIN_PASSWORD` 缺失 →
007 跳过 seed（而不是种出一个人尽皆知的口令）。

⚠️ **`nuxt build` 会把构建时的 `.env` 快照进
`.output/server/chunks/nitro/nitro.mjs`**（`_inlineRuntimeConfig`，明文）。
`.output/` 已 gitignore，但**别把构建产物当成可以随便分发的东西**。
要产出不含密钥的包：构建时不加载 `.env`（临时移开或换用 `--dotenv`
指向空文件），运行时再用 `NUXT_*` 环境变量覆盖 —— 空串缺省正是为此留的。

| 变量 | 说明 |
| :--- | :--- |
| `NUXT_DB_HOST` / `NUXT_DB_PORT` / `NUXT_DB_USER` / `NUXT_DB_PASSWORD` / `NUXT_DB_NAME` | MySQL 连接。⚠️ 纯数字密码会被 nuxt 自动转型，服务端已 `String()` 兜底 |
| `NUXT_DB_SYNCHRONIZE` | 根据实体自动同步表结构，默认 `false`，生产保持关闭 |
| `NUXT_DB_MIGRATIONS_RUN` | 启动时执行尚未执行的迁移，默认 `true` |
| `NUXT_DB_LOGGING` | 输出 TypeORM 数据库日志，默认 `false`；修改后重启 Web 生效 |
| `NUXT_JWT_SECRET` | JWT HS256 密钥，`openssl rand -hex 32` 生成 |
| `NUXT_ADMIN_USERNAME` / `NUXT_ADMIN_PASSWORD` | 初始管理员，仅 007 seed 首次建库时生效 |
| `NUXT_AUTH_COOKIE_SECURE` | refresh Cookie 的 `Secure` 属性。生产 HTTPS 必须 `true`；本地 http 调试置 `false`，否则浏览器/curl 不回传 Cookie |
| `NUXT_RUNNER_URL` / `NUXT_RUNNER_TOKEN` | Runner 地址与 `X-Internal-Token` 共享密钥（与 Runner 的 `COLAB_RUNNER_TOKEN` 一致），URL 缺省 `http://127.0.0.1:8765` |
| `NUXT_REPO_ROOT` | 仓库根（`workflows/*.json` 同步源）；缺省回退 `<cwd>/..`（dev 下 cwd=web/），生产必须显式配置 |
| `NUXT_RUNNER_OUTPUT_DIR` | Runner 输出目录，须与 Runner 的 `COLAB_OUTPUT_DIR` 一致（同机共享目录，log/output 由 Nitro 直读 sendStream，doc/01 §6）；缺省 `<repoRoot>/colab_output` |

## 首次 admin 来源与强制改密

migration `007_seed_admin_user` 从 env 读初始账号建 `role=admin` 用户，
`mustChangePassword=true`：首次登录后强制走
`POST /api/auth/change-password`（改密成功即撤销该账号全部 refresh token，
需用新密码重新登录）。admin 新建/重置密码的用户同样置 `mustChangePassword=true`。

## 依赖选型记录

- **密码哈希：`argon2`（npm 包）**。doc/03 §2 指明 argon2id 优于 bcrypt。
  node 26 / linux x64 的 NAPI prebuilt 可直接加载（已实测 hash/verify 通过），
  未触发源码编译，无需回退 `@node-rs/argon2`。参数取 OWASP 基线 m=19MiB/t=2/p=1。
- **JWT：`jsonwebtoken`**。Nitro 跑在 Node 运行时，`jsonwebtoken` 的同步
  sign/verify API 最简、最成熟；若未来迁 edge/WebCrypto 运行时，再换 `jose`。
- **TypeORM 实体用 `EntitySchema` 而非装饰器**：esbuild/nitro 对
  `emitDecoratorMetadata` 支持不全，EntitySchema 零装饰器依赖，实体仍是
  schema 真相（默认 `synchronize: false`，生产变更通过 migration）。
- 请求校验未引入 zod：端点少，手工校验即可（P2 范围内保持依赖最小）。

## 认证机制（doc/02 §3.1）

- access token：JWT HS256，15 分钟，仅存 Pinia 内存（`stores/auth.ts`），
  载荷 `{ sub, username, role, type:'access' }`。
- refresh token：JWT（`jti` = `refresh_tokens.id`），7 天，
  `HttpOnly; Secure; SameSite=Strict; Path=/api/auth` Cookie；
  SHA-256 哈希入库（**明文绝不出库/日志**），支持撤销。
- **轮换 + 重用检测**（doc/03 §2）：刷新时旧 token 立即 `revokedAt` 置位并签发
  新 token；收到已撤销 token → 判定泄露 → 撤销该用户全部 refresh token。
- 全局中间件 `server/middleware/auth.ts`：除 `/api/auth/login`、
  `/api/auth/refresh` 外全部 `/api/**` 过 JWT。
- role 守卫 `requireRole(event, [...])`（`server/utils/auth.ts`）：
  用户管理限 `admin`；P3 用 `['admin','operator']` 把 `viewer` 挡在
  `POST /api/tasks` 外。
- 前端 `$apiFetch`（`plugins/apiFetch.ts`）：401 → 自动打
  `/api/auth/refresh` → 重放原请求；**并发 401 合流成一次刷新**
  （shared promise，避免误触重用检测）；刷新失败 → 清 store 跳 `/login`。

## 代码规范

`npm run lint` = eslint + stylelint + `nuxt typecheck`（三者缺一不可，
`npm run typecheck` 可单独跑）。单测 `npm test`。

- stylelint 的 4 个 peer 依赖（`postcss-html` / `stylelint-config-html` /
  `stylelint-config-recommended-vue` / `stylelint-config-standard-scss`）
  必须装齐，否则 stylelint 直接 ConfigurationError 而不是报违规。
- `nuxt typecheck` 需要 `vue-tsc`（devDependency）；`typescript.strict`
  开着，缺了它 strict 只在编辑器里生效，构建不校验类型。
- stylelint `selector-class-pattern` 强制
  `^container_(dash|task|workflow|session|user|login)_[a-z]+_[a-z]+_[a-z]+$`
  （doc/04 §1.4）；状态用 `data-*` 属性（如 `[data-status="recoverable"]`）。
- 自定义 eslint 规则 `colab/no-scoped-style`（`eslint-rules/no-scoped-style.mjs`）
  禁用 `<style scoped>`（doc/04 §1.3）。

## 验证记录（2026-08-22，真实 MySQL 192.168.1.25/colab_task）

### migration

`SHOW TABLES` → `migrations` / `refresh_tokens` / `users`；
`users` 含 `mustChangePassword tinyint(1) NOT NULL DEFAULT 0`；
`refresh_tokens` 含索引 `IDX_refresh_tokens_user_revoked (userId, revokedAt)`、
`IDX_refresh_tokens_expires (expiresAt)` 与 `ON DELETE CASCADE` 外键；
admin 已 seed（`role=admin`，`mustChangePassword=1`，
`passwordHash` 前缀 `$argon2id$v=19$m=194...`）。二次启动幂等（不重建不重复 seed）。

### curl 全流程（dev server；当时端口 3001，现默认 54471）

| # | 场景 | 命令要点 | 结果 |
| :-- | :--- | :--- | :--- |
| 1 | 无 token | `curl $B/api/auth/me` | 401 `UNAUTHORIZED 缺少访问令牌` |
| 2 | 错误密码 | `POST /api/auth/login` | 401 `UNAUTHORIZED 用户名或密码错误` |
| 3 | admin 登录 | `POST /api/auth/login -c jar` | 200 `{accessToken, user}`，`mustChangePassword=true`，Set-Cookie `colab_refresh` |
| 4 | me | `GET /api/auth/me -H Bearer` | 200 `{user}` |
| 5 | refresh 轮换 | `POST /api/auth/refresh -b jar -c jar` | 200 新 accessToken，Cookie 已轮换 |
| 6 | **旧 refresh 重用** | `POST /api/auth/refresh -b jar_old` | 401 `检测到刷新令牌重用…`，**全量撤销生效**（新 Cookie 再刷也 401） |
| 7 | 首次改密 | `POST /api/auth/change-password` | 200 `mustChangePassword=false`；旧密码 401、新密码 200 |
| 8 | admin 建 viewer | `POST /api/users` | 201，`mustChangePassword=true` |
| 9 | viewer 越权 | `GET/POST /api/users`（viewer token） | 403 `FORBIDDEN`；`GET /api/auth/me` 200 |
| 10 | PATCH 停用 | `PATCH /api/users/2 {isActive:false}` | 200，viewer refresh 即刻 401 |
| 11 | logout | `POST /api/auth/logout` | 200，refresh Cookie 撤销（再刷 401） |

错误响应均为统一格式 `{ error: { code, message, detail } }`（doc/02 §3.4）。
验证结束后：测试用户已删（FK CASCADE 连带其 refresh_tokens），
admin 密码已改回 seed 初始值，5 条 revoked 的 refresh_tokens 保留作审计。

## 添加 LBH + LightX2V 两阶段工作流

已适配 `workflows/spark_h3_lbh_official_lightx2v_4step_5p2s_i2va.json`：
原文件保留为 ComfyUI 画布参考；Web / Runner 扫描时会跳过画布 JSON。
实际运行入口为
[`workflows/spark_h3_lbh_lightx2v.json`](../workflows/spark_h3_lbh_lightx2v.json)，
API 图位于
[`flows/spark_h3_lbh_official_lightx2v_4step_5p2s_i2va.api.json`](../flows/spark_h3_lbh_official_lightx2v_4step_5p2s_i2va.api.json)。

1. 更新并重启 Web / Runner。直接部署时在 `web/` 执行
   `npm run build`，再重启服务；Docker 部署在 `colab_cli/` 执行
   `docker compose --env-file web/.env up -d --build`。
2. 以 admin / operator 登录，进入「提交任务 → 工作流与模型」，点击「从文件同步」。
3. 返回提交表单，选择「MiniMax H3 LBH + LightX2V 图生视频（4+3 步，兼容版）」。
4. 在「输入图片」中上传首帧，修改视频 Prompt、时长、随机种子后提交。
   「启用尾帧」默认关闭；开启后可再上传尾帧或使用已保存的尾帧。
   关闭时两轮 conditioning 都不使用尾帧，已保存的旧尾帧也不会被带入任务。
   原画布中的两张图片处于旁路状态，API 适配版已接入两轮 conditioning。

工作流配置通过 `optional_last_frame: true` 声明支持开关，任务请求使用布尔值
`use_last_frame`；省略或传 `false` 时默认只使用首帧。Runner 在收集素材之前，
从任务内存副本中移除两处 `last_frame` 连线及不再被使用的尾帧加载节点，
并将结果写入任务 `api.json` 快照；共享 API 图和原图片不受影响。
CLI 可通过 `--use-last-frame` 开启，其他未声明该选项的工作流保持原行为。

默认低分辨率画布 608×352（0.2 MP），先采样 4 步；仅视频 latent 经过 LBH
放大到 1344×768（0.98 MP），音频 latent 原样传递，再采样 3 步，输出带音频的视频。
24 FPS，默认 124 帧（约 5.2 秒），表单也可选 243 / 362 帧；两轮共用提示词、帧数和种子。

Runner 会安装 [LBH 节点](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler)，
并下载原图的完整 INT8 主模型、文本编码器、两个 VAE、
[LightX2V LoRA](https://huggingface.co/Kijai/MiniMax-H3_comfy/tree/main/loras) 和
[LBH v1 FP16 放大模型](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler/tree/main/minimax_h3_latent_upscaler_3d_conv_v1)。
配置中的六个模型共约 **54.2 GiB**，可复用 Drive / VM 缓存。
Colab 缺失模型时优先直接下载到已挂载且可写的 Drive，校验后按 `cache_path`
归档。推理前优先暂存成 VM 本地文件；空间不足时保留 Drive 链接。
Drive 不可用时，在确认本地下载空间并预留 2 GiB 后回退 VM 下载。
提交页的“模型获取方式”可选择自动、直接下载、Drive 软链接或 Drive 复制，
无需为单个工作流增加开关；继续使用“挂载 Google Drive”选项控制持久缓存。
ComfyUI 需具备原生 MiniMax H3、ResolutionSelector、ComfyMathExpression 等节点
（原图使用 ComfyUI 0.33.2）；复用旧 VM 前应先更新 ComfyUI。
权重大小不等于运行显存，具体 GPU 的显存 / 内存是否足够仍需实机验证。

本适配版关闭原图的 `MiniMaxH3SparkAttentionSM120` 和强制 Kitchen Attention，
使用 ComfyUI 默认注意力后端。[Spark 的 ComfyUI 发行包](https://github.com/zechengtang/Spark-H3/blob/main/comfyui/README.md)
需要匹配 SM86 / SM89 / SM120、CUDA 和 ComfyUI 版本；当前 Web 的云 GPU 选项不能
统一套用 SM120 节点。若使用支持 Spark 的设备，需要另行安装匹配包并接回相应架构的节点。

## P3 任务主链路 API（doc/02 §3.2）

全部需 JWT；写操作限 `admin`/`operator`（`viewer` 403 FORBIDDEN）。

```http
GET    /api/workflows                        → { items: WorkflowConfig[] }
POST   /api/workflows/sync                   → { synced, slugs[] }   # 文件→表，operator+
POST   /api/tasks                            → 202 { task_id, status: "queued" }
GET    /api/tasks?status=&page=&size=        → { items, total, page, size }   # size≤100，queuedAt 倒序
GET    /api/tasks/:id                        → { task }                # DB 行即真相
GET    /api/tasks/:id/events?from_seq=N      → SSE（详见下节）
GET    /api/tasks/:id/log                    → text/plain 附件（run.log 直读共享目录）
GET    /api/tasks/:id/output                 → application/zip 附件 + X-Output-Sha256 头
POST   /api/tasks/:id/cancel  { mode }       → Runner 原样：200 canceled / 202 canceling
POST   /api/tasks/:id/redownload             → 202 { task_id, status: "redownloading" }，仅 recoverable
GET    /api/sessions                         → { items, generatedAt }（Runner 实况 ⋈ sessions 表 ⋈ 在途任务）
POST   /api/sessions                         → 201 启动 Colab VM（operator+，body: name + accelerator + mount_drive）
POST   /api/sessions/reconcile               → 对账摘要（operator+）
DELETE /api/sessions/:name                   → { ok, name, log }（operator+）
DELETE /api/sessions/by-endpoint/:endpoint   → 名称映射丢失时按 endpoint 停止
```

`POST /api/tasks` 内部流程：校验 body（doc/02 §2.3 字段 + `dry_run` 透传）→
Runner 健康检查（失败 503 `RUNNER_UNAVAILABLE`）→ workflow **sync-on-demand**
（行缺失或文件 mtime 晚于 syncedAt 时先同步，避免 FK 失败）→ 加速器兼容性前置校验
（400 `ACCELERATOR_UNSUPPORTED`）→ 转发 Runner（POST 不重试，409 `SESSION_BUSY`
原样透传）→ 用 Runner 返回的 task_id 作 Task 主键建行 → 启动 SSE 订阅。

Task 表关键点：`id char(24)` 非自增（Runner 生成的 task_id）；`activeSessionKey`
**STORED 生成列** `IF(status IN ('queued','running'), sessionName, NULL)` + 唯一索引，
实现「同一 session 只允许一个非终态任务」（MySQL 无部分唯一约束；这是 Runner 409
之后的第二道防线，撞索引时 Console 会尽力 cancel Runner 侧任务再报 409）。
`transportMode` 枚举在 doc/03 的 `('native','cli')` 基础上扩了 `'dry_run'`
（演练通道终态回报 `transport: "dry_run"`，不入枚举会回写失败）。

### SSE 订阅管理器（`server/services/taskSubscription.ts`，doc/02 §3.3）

**订阅由任务生命周期驱动，不由浏览器连接驱动**。每个非终态任务一条
Nitro→Runner SSE 长连接（`ensureTaskSubscription`，幂等），从受理持续到终态：
事件①原样广播给浏览器订阅者（含 progress/metrics/log.line）②`start/ok/warn/error`
经 `taskEvents.persistMilestoneEvent` 入库（`(taskId,seq)` 唯一索引去重，INSERT IGNORE）
③终态事件回写 tasks 行（status/finishedAt/durationMs/transportMode/outputZipPath/
sha256/sessionKept/error…；failed/canceled 且 session_kept → `recoverable`）。
每个任务串行处理事件；事件、任务终态与水位在同一事务中提交，成功后才广播并推进续订水位。数据库失败会关闭当前流并重放，旧事件不能覆盖较新的状态。

- 断线重连：流关闭未见终态 → 指数退避（1s→30s）以 `from_seq=<已处理最大 seq>` 续订；
  Runner 侧 events.jsonl 落盘回放补漏。零事件空关流的异常情形不复位退避（防 1s 热循环）。
- 启动补拉（`plugins/03.task-recovery.ts`）：对所有 `queued`/`running` 行以
  `from_seq=tasks.lastSeq` 重建订阅，覆盖 Nitro 宕机窗口；Runner 健康检查失败仅告警
  （probe 降级也会告警，doc/01 §5 R1），提交任务时才 503。
- Runner 已无记录（404 且 events.jsonl 不在）→ 任务标 `orphaned`（RUNNER_LOST）。
- 每轮对账检查全部在途任务，不依赖 VM 是否活着或上一轮的 `expectedAlive`。明确 404
  标记 `RUNNER_LOST`，明确 interrupted 标记 `RUNNER_INTERRUPTED`；查询失败保留状态，
  下一轮继续核实。VM 存活但无人接管时保留计费告警。误标 `SESSION_LOST` 的任务会在
  对账时核实原任务编号、会话与事件水位，恢复状态并从已入库水位继续补拉。
- 任务详情在 SSE 建连、阶段变化时刷新摘要；可见页面每 15 秒校准运行状态，
  误标任务恢复后自动重建浏览器事件流。工作流节点进度单独显示并同步进实时日志。
- Web→Runner 与浏览器→Web 的 SSE 通道超过 45 秒无响应会重连；资源采样超过 90 秒
  未更新提示数据过期，心跳不算资源采样更新。
- `model_cache` 由 `model.prepare` 事件 data 尽力 upsert（含 progress 事件，不入
  task_events 但可对账）；comfyDir/cachePath 事件里没有，取 filename 首段/本身。
- 事件入库过滤器在 Repository 层断言（`assertStorableEvent`）：拒绝 `status='progress'`
  与 `phase IN ('metrics.sample','log.line')`，不靠调用方自觉。

浏览器侧 `GET /api/tasks/:id/events?from_seq=N`：先挂订阅缓冲实时帧 → 从
task_events 表回放 seq>N 的里程碑（`event: replay`）→ 缓冲帧按 seq 去重补发 →
发送最新任务状态（`event: snapshot`）→ 实时透传（`event: task`）。历史失败不会
关闭重试下载的连接，只有当前终态或最新终态快照才收尾；
终态事件送达即关闭（与 Runner 语义一致）；任务已终态则回放后立即关闭。
浏览器断开只摘订阅，不动 Nitro→Runner 长连接。

## 遗留风险 / TODO

- **Secure Cookie 与 http**：`NUXT_AUTH_COOKIE_SECURE=false` 时 Cookie 不带
  Secure —— 现在监听 `0.0.0.0`，局域网内是**明文 http 传 refresh Cookie**，
  仅限可信内网调试；部署到 HTTPS 必须改回 `true`。
- **JWT role 最长 15 分钟陈旧**：中间件信任 access token 里的 role 声明；
  改角色/停用即时撤销 refresh，但已签发的 access token 要等过期
  （`/api/auth/me` 与用户管理出参是实时查库的）。
- 过期 `refresh_tokens` 由 `plugins/05.refresh-token-cleanup.ts` 每日清理
  （只删 `expiresAt < now`；未过期已撤销的行保留到过期，供重用检测）。
- 端口 54471 被占用时 dev 自动顺延（get-port 行为），此时 dev 与
  `npm start`（固定 54471）会跑在不同端口上，注意别对错服务。
- P3 后端已完成并经真实 MySQL + Runner（dry_run）端到端验证（2026-08-22）：
  migration 002–006、`POST /api/tasks`（viewer 403 / 加速器 400 / Runner 409 透传）、
  生命周期驱动的 SSE 订阅（浏览器实时帧含 progress/metrics.sample/log.line，
  DB 只入库里程碑）、启动补拉（Nitro 宕机窗口内完结的任务重启后补齐到 succeeded）、
  cancel 双模式（interrupt→recoverable / destroy→canceled）、output/log 直读共享目录、
  Runner 停机 → 503 RUNNER_UNAVAILABLE。
- Runner redownload：`cancel_requested` 复位；终态任务不在内存时从
  `events.jsonl` 水合并续接 seq；redownload 期间占 session 锁。
  回归：`tests/test_runner.py` 的 `test_redownload_after_cancel` /
  `test_redownload_after_registry_miss`。
- `tasks.exitCode` 契约中无数据源（Runner 终态事件不含 exit_code），暂恒为 NULL。
- P4/P5 已落地（对账规则单测 + 页面）。M3 真实孤儿 VM 检出、M4 `start.sh` 退休
  仍需 Colab 环境与使用切换。
