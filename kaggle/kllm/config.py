"""kllm 网关配置: 全部从环境变量读取, 无配置文件。"""

import os
from dataclasses import dataclass
from pathlib import Path

# 模型代理的 OpenAI 兼容端点在 MODEL_PROXY_URL 之后还有 /openapi 一段,
# 见 kaggle_benchmarks/kaggle/model_proxy.py:94。
OPENAI_PATH_SUFFIX = "/openapi"

# Kaggle CLI 客户端硬编码的可用模型列表(kaggle_api_extended.py:9789)。
# 上游没有 GET /models 接口(实测 404), 只能用这份静态列表。
AVAILABLE_MODELS = (
    "anthropic/claude-sonnet-5@default",
    "deepseek-ai/deepseek-r1-0528",
    "google/gemini-3-flash-preview",
    "google/gemini-3.1-flash-lite-preview",
    "ibm/granite-4.0-h-small",
    "openai/gpt-5.4-nano-2026-03-17",
    "openai/gpt-oss-120b",
    "qwen/qwen3-next-80b-a3b-instruct",
)

DEFAULT_MODEL = "google/gemini-3-flash-preview"

# Kaggle AI 额度上限(美元), 用于 kllm usage 的余量估算。
DAILY_BUDGET_DOLLARS = 10.0
MONTHLY_BUDGET_DOLLARS = 100.0


@dataclass(frozen=True)
class Settings:
    """网关运行参数。"""

    host: str
    port: int
    local_key: str | None
    cache_dir: Path
    request_timeout_seconds: float

    @property
    def credentials_path(self) -> Path:
        """凭据缓存文件路径。"""
        return self.cache_dir / "credentials.json"

    @property
    def usage_path(self) -> Path:
        """用量账本文件路径。"""
        return self.cache_dir / "usage.jsonl"


def load_settings() -> Settings:
    """从环境变量构造 Settings。"""
    default_cache = Path.home() / ".cache" / "kllm"
    return Settings(
        host=os.getenv("KLLM_HOST", "127.0.0.1"),
        port=int(os.getenv("KLLM_PORT", "8799")),
        local_key=os.getenv("KLLM_LOCAL_KEY") or None,
        cache_dir=Path(os.getenv("KLLM_CACHE_DIR", str(default_cache))),
        request_timeout_seconds=float(os.getenv("KLLM_TIMEOUT", "600")),
    )
