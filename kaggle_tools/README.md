# kaggle_tools

把 Kaggle Benchmarks 的 AI 额度（$10/天、$100/月）用起来的两个工具：

| 命令 | 作用 |
|---|---|
| `kllm` | 本地 **OpenAI 兼容网关**（`http://127.0.0.1:8799/v1`），自动续期 1 小时过期的代理 token，给 aider / Cline / Continue / opencode 用 |
| `kcloud` | 云端代码生成评测：把本地仓库传成私有 Dataset → 推 Benchmark Task → 模型在云端改代码并跑测试 → 下载 `changes.diff` |

> ⚠ 这笔额度设计用途是 Benchmarks 评测，当通用编码后端属于超出预期用法，可能被限流或收回。详见 [docs/kllm.md](docs/kllm.md#已知风险)。

## 安装

需要 Python ≥ 3.11。推荐 [uv](https://docs.astral.sh/uv/)（按 `uv.lock` 精确安装）：

```bash
cd kaggle_tools
uv sync --extra dev                 # 创建 .venv 并安装锁定版本
```

或用 pip：

```bash
# bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock.txt && pip install -e . --no-deps
```

```powershell
# Windows PowerShell
py -3.11 -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.lock.txt; pip install -e . --no-deps
```

Kaggle 认证：`kaggle auth login`（或把 `access_token` / `kaggle.json` 放到 `~/.kaggle/`，Windows 是 `%USERPROFILE%\.kaggle\`），用 `kaggle config view` 确认能看到用户名。

## 配置

全部配置都从环境变量读取，也支持当前目录的 `.env` 文件（真实环境变量优先）。复制模板：`cp .env.example .env`（PowerShell：`Copy-Item .env.example .env`）。非法值启动时直接报错并指出变量名。

| 变量 | 默认 | 说明 |
|---|---|---|
| `KLLM_HOST` / `KLLM_PORT` | `127.0.0.1` / `8799` | 网关监听地址 |
| `KLLM_LOCAL_KEY` | 空 | 本地访问密钥，空表示不校验 |
| `KLLM_CACHE_DIR` | `~/.cache/kllm` | 凭据缓存与用量账本 |
| `KLLM_TIMEOUT` | `600` | 上游请求超时（秒） |
| `KLLM_DEFAULT_MODEL` | `google/gemini-3-flash-preview` | `kllm ask/doctor` 默认模型 |
| `KLLM_MAX_RETRIES` / `KLLM_RETRY_BASE_DELAY` / `KLLM_RETRY_MAX_DELAY` | `3` / `1.0` / `30` | 重试次数与退避（指数退避，遵守 `Retry-After`） |
| `KLLM_RETRY_STATUSES` | `429,503` | 重试的状态码。500/502 时上游可能已计费，默认不重试 |
| `KLLM_DAILY_LIMIT` / `KLLM_MONTHLY_LIMIT` | `10` / `100` | 本地额度上限（美元），达到后返回 429；`0` 表示不限制 |
| `KLLM_BUDGET_WARN_RATIO` | `0.8` | 达到上限这个比例时告警 |
| `KLLM_TIMEZONE` | `Asia/Shanghai` | "今天/本月"按哪个时区切分 |
| `KLLM_MAX_CONCURRENCY` | `4` | 同时转发给上游的请求数，多出的排队 |
| `KLLM_FALLBACK_MODELS` | 空 | 备用模型（逗号分隔），首选模型 429/5xx/网络失败时依次尝试 |
| `KLLM_LOG_LEVEL` / `KLLM_LOG_FILE` | `INFO` / 空 | 日志级别、可选日志文件 |
| `KCLOUD_DEFAULT_MODEL` | `anthropic/claude-sonnet-5@default` | kcloud 默认模型 |
| `KCLOUD_RUNS_DIR` | `./runs` | kcloud 产物目录 |
| `KCLOUD_RUN_WAIT` / `KCLOUD_PUSH_WAIT` | `900` / `600` | 云端等待秒数 |
| `KAGGLE_CLI_TIMEOUT` | `1800` | 单条 kaggle 命令的超时 |

## kllm：本地网关

```bash
kllm doctor                    # 自检：拿凭据 + 真调一次模型
kllm serve                     # 启动网关（非本机地址必须设 KLLM_LOCAL_KEY，或显式 --insecure）
kllm models                    # 可用模型
kllm models --check            # 逐个真实调用检查可用性（消耗少量额度）
kllm ask "写个快排" -m anthropic/claude-sonnet-5@default
kllm usage                     # 本地估算的今日/本月用量与余额（按 KLLM_TIMEZONE 切分）
```

客户端配置：Base URL `http://127.0.0.1:8799/v1`，API Key 随便填（设置了 `KLLM_LOCAL_KEY` 就填那个值），模型名用 `kllm models` 里的原样字符串。

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8799/v1 OPENAI_API_KEY=unused            # bash
```
```powershell
$env:OPENAI_BASE_URL="http://127.0.0.1:8799/v1"; $env:OPENAI_API_KEY="unused"     # PowerShell
```

网关行为：

- **额度**：达到 `KLLM_DAILY_LIMIT` / `KLLM_MONTHLY_LIMIT` 后返回 429（`type: budget_exceeded`）；达到 80% 时记告警日志，并加 `X-KLLM-Budget-Warning` 响应头。每个响应都带 `X-KLLM-Budget`。`GET /v1/usage` 返回今日/本月用量。
- **重试**：token 失效（401/403）时续期后重试一次；429/503 和连接失败按退避重试；500/502 和读超时默认不重试（避免重复扣费）；网络失败最终返回 502。
- **备用模型**：设置了 `KLLM_FALLBACK_MODELS` 时，首选模型失败会依次换备用模型，实际使用的模型见 `X-KLLM-Model`。
- **流式**：上游中途断开时补发一个 SSE `error` 事件和 `data: [DONE]`，账本记为 `incomplete`。
- **并发**：同时最多 `KLLM_MAX_CONCURRENCY` 个上游请求。
- 每个响应带 `X-Request-ID`，日志记录模型、状态码和耗时。不支持 Claude Code（它要 Anthropic 协议）。

## kcloud：云端代码生成

```bash
kcloud local -r examples/example_repo                        # 1. 本地试跑（只花模型额度），push 前必做
# kcloud local --executor modal -r examples/example_repo     #    同上，但在 Modal Sandbox 里隔离执行（见 ../docs/MODAL.md）
kcloud dataset -r examples/example_repo --dry-run            # 2. 先预览要上传什么
kcloud dataset -r examples/example_repo -y                   #    上传为私有数据集
kcloud push -d <用户名>/example-repo -y                       # 3. 推任务（⚠ 永久，无法删除）
kcloud run                                                   # 4. 云端运行
kcloud status | kcloud log | kcloud download                 # 5. 看结果，下载 changes.diff
kcloud apply -r <本地仓库>                                    # 6. 应用到本地仓库的新分支
kcloud cycle -d <用户名>/example-repo -y                       # 3~5 一条龙（已推送过加 --skip-push）
kcloud build                                                 # 只生成单文件任务到 runs/tasks/
```

每次运行产物放在新目录 `runs/local/<时间戳>/` 或 `runs/cloud/<时间戳-模型>/`，不会覆盖上一次。`runs/latest` 和 `runs/<local|cloud>/latest` 指向最近一次（Windows 无符号链接权限时改为 `latest.txt`）。`kcloud local --resume` 接着 `runs/local/latest` 继续改。

`kcloud apply` 默认取 `runs/latest` 里的 `changes.diff`，先 `git apply --check`（失败时什么都不改），再在新分支 `kcloud/<时间戳>` 上应用并提交。要求工作区干净（`--allow-dirty` 跳过）；`--no-commit` 只应用不提交，`-b` 指定分支名，`--diff` 指定文件。

模型在云端改代码时可用的工具：`list_files`、`read_file(path, start_line, end_line)`、`edit_file(path, old_text, new_text)`、`write_file`、`run_verification`。大文件读取时会提示被截断，也不允许整份覆盖，只能用 `edit_file` 局部修改。

`-t repo|spec|codegen` 选择任务（默认 `repo`）。`repo` 任务可以用 `-p` / `--prompt-file` / `--verify` / `--protected` / `--max-rounds` 在运行时覆盖，不用修改任务文件。完整说明见 [docs/cloud_codegen.md](docs/cloud_codegen.md)，Kaggle 原生命令见 [docs/KAGGLE_CLI.md](docs/KAGGLE_CLI.md)。

下载后**先审查 `changes.diff`**，再用 `kcloud apply` 应用。

## 项目结构

```
src/kaggle_tools/
├── common/      config(集中配置+校验) logging(request id) errors retry kaggle_cli(子进程+超时)
├── kllm/        credentials(续期) server(FastAPI 网关) usage(账本) cli
├── cloud/       cli(kcloud) build(生成单文件任务)
└── tasks_src/   _workspace_tools.py(共享) + repo/spec/codegen 任务源码
tests/unit/      全部离线（httpx.MockTransport），不碰真实服务
tests/integration/  @pytest.mark.live，真实调用、消耗额度
scripts/         build_tasks.py、probe_proxy.py（真实探测）
examples/        示例仓库、提示词、契约
```

**任务必须是单文件**：`kaggle b t push` 只上传一个 `.py`。所以共享代码放在 `tasks_src/_workspace_tools.py`，由 `kcloud build`（或 `python scripts/build_tasks.py`）内联进每个任务；`push` 和 `local` 自动先构建再执行。

## 开发

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest                      # 默认跳过 live 测试
KAGGLE_TOOLS_LIVE=1 uv run pytest -m live    # 真实调用（PowerShell: $env:KAGGLE_TOOLS_LIVE=1）
uv lock && uv export --no-dev --no-hashes --no-emit-project -o requirements.lock.txt   # 改了依赖后
```

CI（`.github/workflows/kaggle-tools.yml`）在 Linux 和 Windows 上跑 ruff + pytest。

## 故障排查

| 现象 | 处理 |
|---|---|
| `找不到 kaggle 命令` | 激活装了本项目的虚拟环境 |
| `无法获取 Kaggle 模型代理凭据` | `kaggle config view` 确认登录；账号要开通 Benchmarks |
| `User location "HK" is not supported` | 地区限制在代理服务端，换出口或联系 Kaggle |
| 频繁 429 | 已自动退避重试；调大 `KLLM_MAX_RETRIES`、降低 `KLLM_MAX_CONCURRENCY`，或配置 `KLLM_FALLBACK_MODELS` |
| 429 `budget_exceeded` | 达到本地额度上限；`kllm usage` 查看，调整 `KLLM_DAILY_LIMIT` |
| `拒绝启动: 监听 0.0.0.0 ...` | 设置 `KLLM_LOCAL_KEY`，或确认风险后加 `--insecure` |
| `kcloud apply` 提示无法干净应用 | 仓库在任务之后有变化；按任务时的版本新建分支再应用，或手动合并 |
| `kaggle ... 超过 N 秒未结束` | 调大 `KAGGLE_CLI_TIMEOUT` |
| 配置错误：`XXX 应为整数` | 检查 `.env` / 环境变量中对应项 |
