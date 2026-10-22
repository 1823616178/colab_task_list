# kllm — 用 Kaggle AI 额度当本地编码模型后端

把 Kaggle Benchmarks 的 AI 额度（$10/天、$100/月）包装成一个本地 **OpenAI 兼容端点**，
让 aider / Cline / Continue / opencode 等编码工具直接用上 `claude-sonnet-5`、`gpt-5.4-nano`、
`gemini-3-flash` 等模型。

## 原理

Kaggle 的「AI 额度」本质是一个 OpenAI 兼容网关，`kaggle benchmarks auth` 拿到的
`MODEL_PROXY_URL` + `MODEL_PROXY_API_KEY` 就是它的地址和密钥，
真正的 OpenAI 兼容路径是 `{MODEL_PROXY_URL}/openapi`
（见 `kaggle_benchmarks/kaggle/model_proxy.py:94`）。

token **1 小时过期**，所以 kllm 的核心工作就两件：自动续期 + 原样转发。

```
编码工具 ──OpenAI 协议──> kllm (127.0.0.1:8799) ──换上活 token──> mp-staging.kaggle.net
```

## 快速开始

```bash
pip install -e .                         # 在 kaggle_tools/ 下
kaggle config view                       # 确认已认证（~/.kaggle/access_token 或 kaggle.json）

kllm doctor                          # 自检：拿凭据 + 真调一次模型
kllm serve                           # 起网关，默认 http://127.0.0.1:8799/v1
```

命令：

| 命令 | 作用 |
|---|---|
| `kllm doctor` | 自检凭据与代理连通性，地区限制等问题会给出诊断 |
| `kllm serve` | 启动网关 |
| `kllm models` | 列出可用模型 |
| `kllm ask "写个快排" -m anthropic/claude-sonnet-5@default` | 不起服务直接问一句 |
| `kllm usage` | 用量与花费估算 |

环境变量：`KLLM_HOST`(127.0.0.1) `KLLM_PORT`(8799) `KLLM_LOCAL_KEY`(本地访问密钥，不设则不校验)
`KLLM_CACHE_DIR`(~/.cache/kllm) `KLLM_TIMEOUT`(600 秒)。

## 接客户端

```bash
export OPENAI_API_BASE=http://127.0.0.1:8799/v1
export OPENAI_BASE_URL=http://127.0.0.1:8799/v1
export OPENAI_API_KEY=unused            # 设了 KLLM_LOCAL_KEY 就填那个值

aider --model openai/anthropic/claude-sonnet-5@default
```

- **Cline / Continue / opencode**：选 "OpenAI Compatible" provider，Base URL 填
  `http://127.0.0.1:8799/v1`，模型名用 `kllm models` 里的原样字符串。
- **openai Python SDK**：`openai.OpenAI(base_url="http://127.0.0.1:8799/v1", api_key="unused")`。
- **Claude Code**：接不上。它要的是 Anthropic `/v1/messages` 协议，本网关只讲 OpenAI 协议。
  要接得额外写一层协议转换（SSE 事件要从 OpenAI chunk 翻成
  `message_start`/`content_block_delta`/`message_stop`，tool_use 结构也不同），
  或者在中间挂一个 LiteLLM。目前**没做**。

## 实测能力（2026-08-28，账号 laycatbot）

| 项目 | 结果 |
|---|---|
| 地区限制 | ✅ 已恢复正常（`demo/benchmarks/bug_report.md` 里的 HK 报错已不复现） |
| `anthropic/claude-sonnet-5@default` | ✅ 可用 |
| tool calling / 多轮 tool 结果回填 | ✅ 可用（编码 agent 的关键路径） |
| 流式 | ✅ Claude 真增量；Gemini 一次性吐完整内容 |
| 流式 usage | ✅ 每个 chunk 都带累计 usage |
| `temperature` | ✅ 接受（虽然 kaggle_benchmarks 自己默认关掉了） |
| `GET {base}/models` | ❌ 404，模型列表只能用 CLI 内置的静态列表 |
| 首包延迟 | 偏高：Gemini ~20s 才出内容，Claude ~2s 出首包、~34s 出正文 |

可用模型（来自 `kaggle_api_extended.py:9789` 的硬编码列表）：

```
anthropic/claude-sonnet-5@default        openai/gpt-5.4-nano-2026-03-17
deepseek-ai/deepseek-r1-0528             openai/gpt-oss-120b
google/gemini-3-flash-preview            qwen/qwen3-next-80b-a3b-instruct
google/gemini-3.1-flash-lite-preview     ibm/granite-4.0-h-small
```

## 用量统计

每次调用记一条到 `~/.cache/kllm/usage.jsonl`，花费取上游返回的 `cost.*_nanodollars`。
**这是本地估算**，只统计经过本网关的调用，真实额度以 Kaggle 平台为准。

## 已知风险

1. **staging 域名 + beta 产品**：端点是 `mp-staging.kaggle.net`，模型列表是 CLI 客户端硬编码的
   （不是服务端下发），升级 kaggle CLI 就可能变。
2. **依赖私有方法**：续期走 `KaggleApi()._fetch_model_proxy_env()`，无稳定性承诺。
   已做兜底：调不通就回落到 `kaggle benchmarks auth` 子进程。`pyproject.toml` + `uv.lock` 精确 pin `kaggle==2.2.4`。
3. **合规**：这笔额度的设计用途是 Benchmarks 任务评测，当成通用编码后端属于超出预期用法，
   可能被限流或收回额度。
4. **速率限制未知**：撞上 429/503 时网关按 `Retry-After`/指数退避重试，仍失败时换 `KLLM_FALLBACK_MODELS` 里的备用模型，都失败才返回给客户端。本地额度上限见 README。

## 开发

见 [../README.md](../README.md#开发)。`scripts/probe_proxy.py` 是 Phase 0 探测脚本，会真实调用（消耗少量额度）。

模块职责：`common/config.py` 配置 / `kllm/credentials.py` 凭据续期 /
`kllm/server.py` FastAPI 转发（重试、502、request id）/ `kllm/usage.py` 账本 / `kllm/cli.py` 命令行。
