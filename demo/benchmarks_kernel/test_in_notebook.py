# 测试: 在 Kaggle Notebook 内通过模型代理密钥调用托管模型
#
# 密钥不写进源码。两种给法：
#   本地：cd demo/benchmarks && kaggle benchmarks init -y && set -a && source .env && set +a
#   Kaggle Notebook：把 MODEL_PROXY_API_KEY 存成 Kaggle Secret 再注入环境变量
# 注意该密钥仅 1 小时有效，过期重跑 `kaggle benchmarks init -y`（见 README）。
import os
import subprocess
import sys

_API_KEY = os.environ.get("MODEL_PROXY_API_KEY")
if not _API_KEY:
    raise SystemExit(
        "错误: 缺少环境变量 MODEL_PROXY_API_KEY，"
        "先跑 `kaggle benchmarks init -y` 再 `source demo/benchmarks/.env`"
    )

os.environ["MODEL_PROXY_URL"] = os.environ.get(
    "MODEL_PROXY_URL", "https://mp-staging.kaggle.net/models"
)
os.environ["LLM_DEFAULT"] = "google/gemini-3-flash-preview"
os.environ["LLM_DEFAULT_EVAL"] = "google/gemini-3-flash-preview"
os.environ["LLMS_AVAILABLE"] = "anthropic/claude-sonnet-5@default,deepseek-ai/deepseek-r1-0528,google/gemini-3-flash-preview,google/gemini-3.1-flash-lite-preview,ibm/granite-4.0-h-small,openai/gpt-5.4-nano-2026-03-17,openai/gpt-oss-120b,qwen/qwen3-next-80b-a3b-instruct"

subprocess.check_call(
    [sys.executable, "-m", "pip", "install", "-q", "kaggle-benchmarks", "protobuf==5.29.6"]
)

import kaggle_benchmarks as kbench

try:
    llm = kbench.llms["google/gemini-3-flash-preview"]
    resp = llm.prompt("Say hello in one word.")
    print("=== SUCCESS ===")
    print(resp)
except Exception as e:
    print("=== FAILED ===")
    print(type(e).__name__, str(e)[:500])
