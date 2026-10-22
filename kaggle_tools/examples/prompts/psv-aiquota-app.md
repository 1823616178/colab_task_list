# PS Vita AI 额度显示器（VitaQuota）—— 设计与任务文档

这一份文件同时是**设计文档**和**任务清单**，直接作为 `cloud_codegen` 的 `repo`
任务提示词使用。本地试跑（算力用自己的，只消耗模型额度）：

```bash
cd kaggle/cloud_codegen
python run_cloud.py -t repo local \
  --repo ./examples/psv_quota \
  --prompt-file ./examples/prompts/psv-aiquota-app.md \
  --output ./runs/psv-quota-local-01 \
  --model anthropic/claude-sonnet-5@default \
  --verify "bash scripts/test.sh" \
  --protected "" \
  --max-rounds 120
```

后三个覆盖项必须给，否则任务会拿默认值跑偏：

| 参数 | 不给会怎样 |
| --- | --- |
| `--verify "bash scripts/test.sh"` | 默认验收命令是 `python -m pytest -q`，这个仓库里没有 pytest 用例，会以 `exit_code=5`（未收集到测试）直接判失败 |
| `--protected ""` | 默认保护 `tests/`，但这个仓库从零生成，测试也得模型自己写 |
| `--max-rounds 120` | 默认 16 轮，只够写两三个文件 |

`scripts/test.sh` 需要 gcc 与 ASan，这条命令请在 WSL 里执行；Windows 侧的 Git Bash
没有可用的 gcc，验收会直接失败。

**一次跑不完整个 §9。** 8 个 `[T-xx]` 合计三十多个源文件，单次 run 的上下文和轮数都
撑不住，模型会写到一半停在「验收还是红的」。实际做法是把本文当设计文档，每次只把
**一个 `[T-xx]` 的段落**（外加 §3 契约与 §13 约束）喂进 `--prompt-file`，产物审完
应用回 `examples/psv_quota/`，再跑下一个 —— 后一轮就能读到前一轮的成果。

产物在 `runs/psv-quota-local-01/`：先看 `verification.txt` 的 `exit_code`，再审
`changes.diff` 与 `workspace/`，确认无误后手动应用到本地仓库——任务不会直接改
原始仓库。

目标仓库 `psv_quota` 初始可以是空目录（只含 `README.md` 和 `.gitignore`）。
本文第 6 节给出的目录结构就是要生成的全部内容。

---

## 1. 目标

在 PS Vita 上做一个原生 homebrew，一屏显示 Claude Code、Codex、Kimi Code 和
Antigravity（Gemini、Claude/GPT）的 5 小时与周额度，数据来自局域网里的
Usage API。

功能等价于 KOReader 插件 `readingquota.koplugin`，工程做法照搬 PS Vita 项目
`psv_soft`（VitaSSH+）已经验证过的那一套。

**范围内**

- 一屏六卡（五个提供商 + 一个接口状态卡），不滚动、不翻页。
- 手动刷新 + 可配置的自动刷新。
- 应用内修改接口地址并持久化。
- 已用 / 剩余两种显示模式切换。
- 手柄与触摸两套操作并存。

**范围外（本次明确不做）**

- HTTPS / TLS：Usage API 是明文 HTTP，不引 openssl。
- 任何写操作：这个应用只 `GET /api/usage`。
- 历史曲线、通知、后台常驻插件（`.suprx`）。
- 多语言：界面固定简体中文 + ASCII。

---

## 2. 参考来源与边界

| 来源 | 提供什么 | 边界 |
| --- | --- | --- |
| `G:\code\koread_ext\readingquota.koplugin` | 字段语义、空值处理、URL 归一化、卡片布局与分段进度条算法 | 只读参考，逐条搬语义，不搬 Lua 代码结构 |
| `G:\code\koread_ext\usage-api.md` | 接口契约，唯一事实来源 | 复制进本仓库 `doc/usage-api.md`，两端共享 |
| `G:\code\psv_soft` | CMake/VitaSDK 工程骨架、SceNet HTTP 客户端写法、主机侧测试方式、VPK 打包与 LiveArea 资源规范 | 只读参考，**不要复制 `assets/ui/vitashell/` 下任何 GPL-3.0 资源** |

需要逐行对照搬过来的三处算法：

1. `quotabar.lua:46-61` 的分段进度条 pitch 取整（保证最后一块正好落在右边界）。
2. `quotapage.lua:39-51` 的尺寸算式（padding / gap / card_width / inner_width）。
3. `quotadata.lua:24-62` 的 `clamp`、`validWindow`、`normalizeUrl`。

---

## 3. 数据契约（摘要，完整见 `doc/usage-api.md`）

```text
GET http://<host>:<port>/api/usage      →  application/json
```

```jsonc
{
  "displayMode": "used" | "remaining",
  "alertPercent": 85,
  "generatedAt": 1785695952333,
  "claude": { "fetchedAt": 0, "five": {"used": 0, "resetAt": 0}, "seven": {} },
  "codex":  {},
  "kimi":   {},
  "agy":    { "fetchedAt": 0,
              "gemini":     { "five": {}, "seven": {} },
              "thirdParty": { "five": {}, "seven": {} } }
}
```

必须遵守的语义（写成断言，主机侧测试逐条覆盖）：

- `used` **恒为已用百分比**，范围 0–100，可能带小数；`displayMode` 不改变它的含义。
- `displayMode == "remaining"` 时显示 `clamp(100 - used)`。
- 任意提供商或窗口可以是 `null`：该项显示「暂无数据」，**不得影响其它卡片**。
- 用类型判断而非真值判断：`0` 是合法的 `used`。
- 未知的新提供商 / 新字段不算错误，忽略即可。
- 提供商失败时接口仍返回 HTTP 200，只有服务端异常才是 500。

---

## 4. 技术选型

### 选定

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 工具链 | WSL + VitaSDK，CMake ≥ 3.16 | Windows 侧没有 `VITASDK`，与 `psv_soft` 保持一致 |
| 渲染 | vita2d，原生 960×544 立即模式 | 单页静态界面，不需要保留模式 UI |
| 网络 | SceNet / SceNetCtl 手写 HTTP/1.1 GET | 无 TLS 无认证，约 200 行；不引 libcurl |
| JSON | vendored cJSON，单独 `add_library` | 嵌套结构 `agy.gemini.five` 用 `strstr` 抠字段不可靠 |
| 字体 | 随包分发 pyftsubset 裁剪的 Noto Sans SC 子集（OFL） | 系统 PGF 的 CJK 覆盖不确定；VitaShell 的 `font.pgf` 是 GPL-3.0，不引 |
| 配置 | `ux0:data/VitaQuota/config.ini`，带版本号，原子写 | 照搬 `vssh_settings.c` 的做法 |

### 排除

- **不用 `strstr` 手抠 JSON**：`psv_soft/src/sync/vssh_sync.c:477` 那套辅助函数
  只适用于平坦对象，本接口有三层嵌套且允许 `null`，会静默读错。
- **不做常驻插件 / LiveArea 小组件**：需要 taiHEN 内核插件，风险与收益不成比例。
- **不依赖设备时钟做倒计时**：见 §7「时间」。

---

## 5. 架构

```text
主线程（UI）                        FetchWorker 线程
  读输入 / 绘制 960x544                 独占 resolver / socket / 响应缓冲
  投递命令  ──── 命令队列 ────►         DNS → connect → GET → 收全响应 → cJSON 解析
  消费事件  ◄─── 事件队列 ────          产出 VqSnapshot 或带阶段的错误
```

硬约束（违反即视为未完成）：

- UI 线程只读输入、绘制、投命令、消事件；socket 与解析全在 Worker。
- **不做每帧堆分配**：`VqSnapshot` 是固定大小结构体，界面无动态字符串拼接缓冲。
- 请求进行中 UI 不阻塞：显示「加载中」并允许 Cross 取消（置 `cancel_requested`，
  Worker 在每个 deadline 检查点退出）。
- 退出时严格逆序释放，每步幂等：Worker 停 → socket 关 → SceNetCtl term →
  SceNet term → 只卸载本应用加载过的模块 → vita2d 退出。
- 命令队列满时，控制命令（取消 / 退出）可以挤掉普通刷新命令。

---

## 6. 目标目录结构

```text
psv_quota/
├── CMakeLists.txt              VitaSDK 交叉编译 + VPK 打包
├── AGENTS.md                   会话交接说明（非协商行为 + 源码地图 + 必跑验证）
├── README.md                   构建、安装、操作、排查
├── include/                    头文件保持扁平
│   ├── vq_config.h  vq_types.h  vq_quota.h  vq_http.h  vq_net.h
│   ├── vq_fetch.h   vq_queue.h  vq_settings.h  vq_ui.h  vq_app.h
│   └── vq_platform.h  vq_log.h  vq_util.h
├── src/
│   ├── app/main.c              入口、模块加载、生命周期
│   ├── app/vq_app.c            状态机（loading/ready/error）、页面路由
│   ├── core/vq_quota.c         JSON → VqSnapshot，空值与百分比语义  ← quotadata.lua
│   ├── core/vq_format.c        百分比、相对重置时间、快照年龄的格式化
│   ├── core/vq_url.c           URL 归一化与 host/port/path 拆解     ← quotadata.lua
│   ├── core/vq_settings.c      config.ini 版本化 + 原子写
│   ├── core/vq_queue.c         有界命令 / 事件队列
│   ├── core/vq_log.c           日志（ux0:data/VitaQuota/log.txt，轮转，脱敏）
│   ├── core/vq_util.c          纯工具函数
│   ├── core/vq_platform_vita.c 时间 / 线程 / 文件的平台抽象
│   ├── net/vq_net.c            SceNet + NetCtl 生命周期与连通性判断
│   ├── net/vq_http.c           非阻塞 TCP + HTTP/1.1 GET + 分块接收
│   ├── net/vq_fetch.c          FetchWorker 编排
│   ├── ui/vq_ui.c              vita2d 生命周期、字体、文本、通用绘制
│   ├── ui/vq_ui_card.c         卡片、分段进度条、状态行            ← quotabar.lua
│   ├── ui/vq_page_board.c      六卡主页
│   ├── ui/vq_page_settings.c   接口地址 / 自动刷新 / 显示模式
│   └── ui/vq_input.c           手柄 + 触摸命中，Circle=确认 Cross=取消
├── third_party/cJSON/          vendored，单独建库，不受本项目告警策略约束
├── assets/fonts/NotoSansSC-subset.ttf
├── sce_sys/icon0.png           128×128，8 位索引色
├── sce_sys/livearea/contents/  bg0.png / startup.png / template.xml
├── scripts/                    build.sh  test.sh  gen_assets.py  gen_font.py
├── tests/test_main.c           主机侧断言
├── tests/vq_platform_host.c    POSIX 平台实现
└── doc/usage-api.md            接口契约副本
```

命名前缀统一 `vq_` / `Vq`，与 `psv_soft` 的 `vssh_` 同构。

---

## 7. 界面设计

### 布局（960×544）

```text
┌─ AI 额度   ●已连接   3 分钟前采集 ──────────────────────────┐  标题栏 40px
│ ┌── Claude Code ────────────┐ ┌── Codex ──────────────────┐ │
│ │ 5 小时      18%           │ │ 5 小时      12%           │ │
│ │ ████░░░░░░░░              │ │ ███░░░░░░░░░              │ │
│ │ 4 小时 38 分后重置        │ │ 4 小时 38 分后重置        │ │
│ │ 周额度      42%           │ │ 周额度      34%           │ │
│ │ ████████░░░░              │ │ ███████░░░░░              │ │
│ └───────────────────────────┘ └───────────────────────────┘ │
│ ┌── Kimi Code ──────────────┐ ┌── AGY Gemini ─────────────┐ │
│ ┌── AGY Claude/GPT ─────────┐ ┌── 接口状态 ───────────────┐ │
└─────────────────────────────────────────────────────────────┘
 O 刷新   △ 显示模式   □ 设置   X 退出                        底栏
```

第六张「接口状态」卡显示：接口地址、`generatedAt` 与各提供商 `fetchedAt` 的相对
时间、`alertPercent`、当前 `displayMode`、最近一次错误（若有）。

尺寸从 `quotapage.lua` 的算式换算到固定像素（不需要 DPI 缩放，分辨率恒定）：

```c
#define VQ_H_PADDING     16
#define VQ_CARD_GAP       8
#define VQ_CARD_PADDING   9
#define VQ_CONTENT_W     (960 - 2 * VQ_H_PADDING)           /* 928 */
#define VQ_CARD_W        ((VQ_CONTENT_W - VQ_CARD_GAP) / 2) /* 460 */
#define VQ_CARD_INNER_W  (VQ_CARD_W - 2 * (VQ_CARD_PADDING + 1))
#define VQ_CARD_H        152
```

字号：卡片标题 22、标签 16、百分比 18、细节 13。

### 分段进度条

照搬 `quotabar.lua`：12 段、gap 2px、高 8px。

- `percentage <= 0` → 0 块；`>= 1` → 全亮。
- 中间值 `round(p * segments)`，但夹在 `[1, segments-1]`：有一点进度就至少亮一块，
  只有满额才亮最后一块。
- 每块左右边界由同一个 `pitch = (width + gap) / segments` 取整推出，保证块宽均匀
  且最后一块正好落在 `x + width`。

窗口为 `null` 时进度条画成整条空色（深一档的灰），值显示 `--`，细节行「暂无数据」。
`used >= alertPercent` 时数值后追加 `!` 并用告警色。

### 操作

| 按键 | 主页 | 设置页 |
| --- | --- | --- |
| ○ Circle | 刷新 | 确认 / 进入编辑 |
| × Cross | 退出（请求中则先取消） | 返回 |
| △ Triangle | 切换已用 / 剩余 | — |
| □ Square | 打开设置 | — |
| SELECT | 打开日志页 | — |

触摸：点标题栏右侧的刷新图标等同 Circle；点某张卡片展开该提供商的 `fetchedAt`
明细（接口是整体拉取的，不提供单卡片刷新）。设置页的地址输入调用系统 IME
（`SceIme`），IME 内同样是 Circle 确认、Cross 取消。

阻塞式菜单和 IME 必须调用注册的嵌套 pump，否则 Worker 事件会在主循环暂停期间堆积。

### 时间

**不依赖 Vita 的墙钟。** 设备时钟可能没校准，直接用 `resetAt - now` 会算出荒唐值。

以服务端时间为基准，本地只累加单调时间：

```c
/* 拉取成功时记下 */
snap->generated_at_ms = json_generated_at;
snap->local_anchor_us = sceKernelGetProcessTimeWide();

/* 渲染时 */
int64_t elapsed_ms = (sceKernelGetProcessTimeWide() - snap->local_anchor_us) / 1000;
int64_t remain_ms  = window->reset_at_ms - (snap->generated_at_ms + elapsed_ms);
```

显示为相对时间：`即将重置` / `x 分钟后重置` / `x 小时 y 分后重置` / `x 天后重置`；
`resetAt` 为 `null` 时显示「重置时间未知」。标题栏的快照年龄同理。

---

## 8. 配置与网络

### config.ini

```ini
version=1
api_url=http://123.161.179.32:46927/api/usage
auto_refresh_sec=300
display_mode=api        ; api | used | remaining
```

- 写入走「临时文件 + rename」，任何页面都不得直接写 `config.ini`。
- 版本号不匹配时安全回退到默认值，不崩。
- URL 归一化照搬 `quotadata.lua:49-62`：去首尾空白 → 空则用默认 → 缺协议补
  `http://` → 去尾部斜杠 → 缺 `/api/usage` 则补上。既接受 `1.2.3.4:46927`
  也接受完整地址。

### 网络生命周期

1. 启动时 `sceSysmoduleLoadModule(SCE_SYSMODULE_NET)` → `sceNetInit` → `sceNetCtlInit`。
2. 每次拉取前查 `sceNetCtlInetGetState`，未连上直接报「未连接 WiFi」，
   **不要让用户干等 50 秒超时**。
3. `sceNetResolverStartNtoa` 解析主机，DNS 超时 10 秒，1 秒粒度可取消。
4. `SCE_NET_SO_NBIO` 非阻塞 connect，TCP 超时 10 秒。
5. 请求头固定 `Connection: close`；响应缓冲按需增长，上限 256 KiB，超限报错。
6. **接收总超时 50 秒**：Kimi 与 AGY 首次采集要临时拉起本地服务，缓存命中后才快。
7. 自动刷新默认 300 秒、最低 60 秒，与服务端缓存策略对齐，不要更激进。
8. 只在请求进行中调用 `sceKernelPowerTick` 阻止息屏；空闲期允许自动待机，
   从待机恢复且快照年龄超过自动刷新间隔时自动重拉一次。

### 错误处理

错误必须带阶段和可重试性，界面显示人话而不是错误码：

| 阶段 | 显示 |
| --- | --- |
| 未连 WiFi | 「未连接网络，请先在系统设置里连接 WiFi」 |
| DNS | 「无法解析主机 `<host>`」 |
| TCP | 「连接 `<host>:<port>` 失败」 |
| HTTP 非 200 | 「接口返回 HTTP `<code>`」 |
| JSON 非法 | 「返回内容不是合法的 JSON」 |
| 用户取消 | 「已取消」 |

拉取失败时**保留上一次成功的快照**并标记为「已过期」，不要清空成一片「暂无数据」。

---

## 9. 任务清单

每个任务给出交付物和验收方式。`[T-xx]` 之间的依赖已标注，允许并行的不要串行做。

### [T-01] 工程骨架与构建 —— 依赖：无

- `CMakeLists.txt`：C11、`-Wall -Wextra -Werror=return-type
  -Werror=implicit-function-declaration -O2`、`CMAKE_EXPORT_COMPILE_COMMANDS ON`。
- cJSON 单独 `add_library(cjson STATIC ...)`，只给 `-O2`，不套本项目告警策略。
- 打包宏照搬 `psv_soft` 的 `vssh_create_self` / `vssh_create_vpk`：最后一步用带
  重试的 `cp` 代替 `cmake -E copy`（WSL drvfs 上有时序问题）。
- `-DVQ_NO_LIVEAREA=ON` 产出只含 `eboot.bin + param.sfo + icon0.png` 的最小 VPK，
  用于定位安装失败。
- `TITLE_ID = AIQU00001`，`VITA_APP_NAME = "VitaQuota"`。
- `scripts/build.sh`、`scripts/test.sh`。

**验收**：`scripts/build.sh` 产出 `build/vitaquota.vpk`；`scripts/test.sh` 通过
（此时测试为空壳也算）。

### [T-02] 纯逻辑核心 —— 依赖：T-01

`vq_quota.c` / `vq_format.c` / `vq_url.c` / `vq_util.c` / `vq_queue.c`，全部不碰
Vita API，可在主机上编译测试。

**验收**：`scripts/test.sh`（gcc + `-fsanitize=address,undefined`）全绿，覆盖
§10 的全部用例。

### [T-03] HTTP 与网络层 —— 依赖：T-01（可与 T-02 并行）

`vq_net.c` / `vq_http.c`：连通性判断、DNS、非阻塞 connect、发全 / 收全、
按需增长缓冲、逐阶段 deadline 与取消。

**验收**：主机侧用 POSIX socket 实现跑通对本地 `python -m http.server` 的 GET；
Vita 侧交叉编译通过。

### [T-04] UI 渲染 —— 依赖：T-01、T-02

`vq_ui.c` / `vq_ui_card.c` / `vq_page_board.c`：字体加载（用户
`ux0:data/VitaQuota/font.ttf` 优先，其次随包子集字体）、六卡布局、分段进度条、
标题栏与底栏。提供编译期开关 `-DVQ_DEMO_DATA=ON`，用 `usage-api.md` 的示例 JSON
渲染，不联网。

**验收**：demo 构建在真机或 Vita3K 上一屏显示六卡，无重叠、无截断；
`src/ui/` 下不出现每帧堆分配。

### [T-05] 拉取编排与状态机 —— 依赖：T-03、T-04

`vq_fetch.c` / `vq_app.c`：命令与事件队列、loading/ready/error 状态、取消、
自动刷新定时、待机恢复后的补刷、失败时保留过期快照。

**验收**：真实接口地址下完成「启动 → 自动加载 → 手动刷新 → 断网重试 → 恢复」，
日志里每次请求都有配对的开始与结束记录。

### [T-06] 输入与设置页 —— 依赖：T-04

`vq_input.c` / `vq_page_settings.c`：手柄映射、触摸命中区、系统 IME 改地址、
自动刷新间隔与显示模式、保存后立即重新拉取。

**验收**：全程只用触摸能完成「改地址 → 刷新 → 返回」；全程只用手柄同样能完成。
Circle / Cross 语义在 IME 内也一致。

### [T-07] 打包资源 —— 依赖：T-01

`scripts/gen_assets.py` 生成 `icon0.png`(128×128)、`bg0.png`(840×500)、
`startup.png`(280×158)，**全部 8 位索引色、带 PLTE 且补齐 256 项**，生成后脚本
自行断言格式。`template.xml` 用 `style="a1"`，`<text>` 只能出现在 `<frame>` 内部。
`scripts/gen_font.py` 用 `pyftsubset` 裁 Noto Sans SC 到「界面用字 + ASCII」。

**验收**：`file sce_sys/icon0.png` 输出含 `8-bit colormap`；完整 VPK 在 VitaShell
里能装上（不出 `0x8010113D`）。

### [T-08] 文档 —— 依赖：全部

`README.md`（构建 / 安装 / 操作表 / 安装失败排查）、`AGENTS.md`（非协商行为、
源码地图、必跑验证）、`doc/usage-api.md`（契约副本）。

**验收**：新会话只读 `AGENTS.md` 就能跑通构建与测试。

---

## 10. 主机侧测试清单（T-02 的验收内容）

`tests/test_main.c` 必须覆盖：

**JSON 语义**

1. `usage-api.md` 的完整示例 → 五个提供商十个窗口全部解析正确。
2. `"used": 0` 被当作有效值，不是缺失。
3. `used` 越界（`-5`、`150`）被 clamp 到 0 / 100。
4. `five: null` → 该窗口标记为无数据，同卡片的 `seven` 仍正确。
5. `agy` 整体为空结构 → 两张 AGY 卡片都显示无数据，其余三张不受影响。
6. `resetAt: null` → 重置时间未知。
7. 出现未知提供商 / 未知字段 → 忽略且不报错。
8. 非法 JSON、空 body、被截断的 JSON → 返回解析错误且不越界读。
9. `displayMode: "remaining"` → 显示值为 `100 - used`，`used` 本身不被改写。

**URL 归一化**

10. `1.2.3.4:46927` → `http://1.2.3.4:46927/api/usage`
11. `http://h:1/` → `http://h:1/api/usage`
12. `http://h:1/api/usage` → 原样
13. 空串 / 全空白 → 默认地址
14. 超长输入不溢出目标缓冲

**格式化**

15. 相对重置时间的四个分支（即将 / 分 / 时分 / 天）与负值（已过期）。
16. 百分比小数的显示位数稳定（如 `0.678` → `0.7%`）。

**队列**

17. 队列满时控制命令挤掉普通命令。
18. 事件队列里连续的 `SNAPSHOT` 合并，错误事件不被丢弃。

---

## 11. 风险表

| 风险 | 影响 | 处置 |
| --- | --- | --- |
| Kimi / AGY 冷启动慢 | 首次拉取可能接近 50 秒 | 全程可取消的 loading 态；文案说明首次较慢 |
| 接口无认证 | 额度属账号信息 | README 明确只在可信局域网使用，不暴露公网 |
| 设备时钟不准 | 倒计时荒唐 | §7 的单调时间锚点方案 |
| PNG 非索引色 | 安装报 `0x8010113D` | `gen_assets.py` 自检 + 最小 VPK 二分定位 |
| drvfs 复制时序 | 构建随机失败 | 打包宏用带重试的 `cp` |
| 系统字体缺字 | 中文显示成方块 | 随包分发子集字体，不依赖系统 PGF |
| 接口未来加版本 / 新字段 | 解析失败 | 按字段读取、忽略未知项、窗口可空 |

---

## 12. 总验收标准

代码改完后**依次**执行，全部通过才算完成：

```bash
bash scripts/test.sh
env VITASDK=/usr/local/vitasdk cmake -S . -B build \
  -DCMAKE_TOOLCHAIN_FILE=/usr/local/vitasdk/share/vita.toolchain.cmake
env VITASDK=/usr/local/vitasdk cmake --build build -j2
git diff --check
```

外加静态检查：

- `-Wall -Wextra` 零告警。
- `src/ui/` 下不出现每帧堆分配。
- `grep -rn "SCE_CTRL_CIRCLE\|SCE_CTRL_CROSS" src/` 只在 `vq_input.c` 命中。
- 任何页面都不直接写 `config.ini`。

真机项（**不得在未上机的情况下声称已完成**，只能写进 README 的「待验证」）：
LiveArea 显示、安装成功、WiFi 断开时的提示、待机恢复后的补刷、长时间挂机不崩。

---

## 13. 给执行模型的约束

- 只新增文件，不修改 `G:\code\koread_ext` 与 `G:\code\psv_soft`（它们是只读参考，
  也不在云端工作副本里；需要的算法已在本文抄录，按描述实现即可）。
- 不引入 libcurl、openssl、SDL 或任何本文未列出的第三方依赖；cJSON 以源码
  vendored 进 `third_party/`。
- 不复制 VitaShell 的任何素材（GPL-3.0）。随包字体必须是 OFL 且在 README 标注来源。
- 代码同时满足 ISO C99 与 C11，不用 `strtok_r` / `strnlen` 这类 POSIX 专有函数。
- 注释写「为什么」，不写「是什么」；界面文案与文档用简体中文。
- 每完成一个 `[T-xx]` 就让 `scripts/test.sh` 保持可跑通，不要留下半截构建。
