"""集中配置: 默认值、模型列表、预算都只在这里定义一次。

读取顺序: 真实环境变量 > 当前目录 .env(或 KAGGLE_TOOLS_ENV_FILE 指定的文件) > 默认值。
所有数值都会校验, 非法值抛 ConfigError 并指出是哪个变量。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

# 模型代理的 OpenAI 兼容端点在 MODEL_PROXY_URL 之后还有 /openapi 一段,
# 见 kaggle_benchmarks/kaggle/model_proxy.py:94。
OPENAI_PATH_SUFFIX = "/openapi"

# Kaggle CLI 客户端硬编码的可用模型列表(kaggle_api_extended.py:9789)。
# 上游没有 GET /models 接口(实测 404), 只能用这份静态列表。
AVAILABLE_MODELS: tuple[str, ...] = (
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
CLOUD_DEFAULT_MODEL = "anthropic/claude-sonnet-5@default"

# Kaggle AI 额度上限(美元), 用于 kllm usage 的余量估算。
DAILY_BUDGET_DOLLARS = 10.0
MONTHLY_BUDGET_DOLLARS = 100.0

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def default_cache_dir() -> Path:
    """凭据缓存与用量账本的默认目录。"""
    return Path.home() / ".cache" / "kllm"


@dataclass(frozen=True)
class Settings:
    """全部运行参数。前五项保持旧签名, 其余带默认值。"""

    host: str = "127.0.0.1"
    port: int = 8799
    local_key: str | None = None
    cache_dir: Path = default_cache_dir()
    request_timeout_seconds: float = 600.0
    default_model: str = DEFAULT_MODEL
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    log_level: str = "INFO"
    log_file: Path | None = None
    cloud_default_model: str = CLOUD_DEFAULT_MODEL
    runs_dir: Path = Path("runs")
    cloud_run_wait_seconds: int = 900
    cloud_push_wait_seconds: int = 600
    kaggle_cli_timeout_seconds: float = 1800.0

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"KLLM_PORT 必须在 1-65535 之间, 收到 {self.port}")
        for name, value in (
            ("KLLM_TIMEOUT", self.request_timeout_seconds),
            ("KAGGLE_CLI_TIMEOUT", self.kaggle_cli_timeout_seconds),
            ("KCLOUD_RUN_WAIT", self.cloud_run_wait_seconds),
            ("KCLOUD_PUSH_WAIT", self.cloud_push_wait_seconds),
        ):
            if value <= 0:
                raise ConfigError(f"{name} 必须大于 0, 收到 {value}")
        if self.max_retries < 0:
            raise ConfigError(f"KLLM_MAX_RETRIES 不能为负数, 收到 {self.max_retries}")
        if self.retry_base_delay < 0 or self.retry_max_delay < self.retry_base_delay:
            raise ConfigError("KLLM_RETRY_BASE_DELAY 须 >= 0 且不大于 KLLM_RETRY_MAX_DELAY")
        if self.log_level.upper() not in LOG_LEVELS:
            raise ConfigError(f"KLLM_LOG_LEVEL 只能是 {'/'.join(LOG_LEVELS)}, 收到 {self.log_level}")
        if not self.host:
            raise ConfigError("KLLM_HOST 不能为空")

    @property
    def credentials_path(self) -> Path:
        """凭据缓存文件路径。"""
        return self.cache_dir / "credentials.json"

    @property
    def usage_path(self) -> Path:
        """用量账本文件路径。"""
        return self.cache_dir / "usage.jsonl"


def parse_env_file(path: Path) -> dict[str, str]:
    """解析一个简单的 .env: KEY=VALUE, 支持 # 注释、export 前缀和引号。"""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"读取 {path} 失败: {error}") from error
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{number} 不是 KEY=VALUE 格式: {raw!r}")
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def merged_environment(environ: Mapping[str, str] | None = None,
                       env_file: Path | None = None) -> dict[str, str]:
    """把 .env 与真实环境变量合并, 真实环境变量优先。"""
    environ = dict(os.environ if environ is None else environ)
    if env_file is None:
        configured = environ.get("KAGGLE_TOOLS_ENV_FILE")
        env_file = Path(configured) if configured else Path.cwd() / ".env"
        if not configured and not env_file.is_file():
            return environ
    elif not env_file.is_file():
        return environ
    merged = parse_env_file(env_file)
    merged.update(environ)
    return merged


def _number(environ: Mapping[str, str], name: str, default, cast):
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        raise ConfigError(f"{name} 应为{'整数' if cast is int else '数字'}, 收到 {raw!r}") from None


def load_settings(environ: Mapping[str, str] | None = None,
                  env_file: Path | None = None) -> Settings:
    """从环境变量(及 .env)构造并校验 Settings。"""
    env = merged_environment(environ, env_file)
    log_file = env.get("KLLM_LOG_FILE", "").strip()
    return Settings(
        host=env.get("KLLM_HOST", "").strip() or "127.0.0.1",
        port=_number(env, "KLLM_PORT", 8799, int),
        local_key=env.get("KLLM_LOCAL_KEY") or None,
        cache_dir=Path(env.get("KLLM_CACHE_DIR") or default_cache_dir()).expanduser(),
        request_timeout_seconds=_number(env, "KLLM_TIMEOUT", 600.0, float),
        default_model=env.get("KLLM_DEFAULT_MODEL", "").strip() or DEFAULT_MODEL,
        max_retries=_number(env, "KLLM_MAX_RETRIES", 3, int),
        retry_base_delay=_number(env, "KLLM_RETRY_BASE_DELAY", 1.0, float),
        retry_max_delay=_number(env, "KLLM_RETRY_MAX_DELAY", 30.0, float),
        log_level=(env.get("KLLM_LOG_LEVEL", "").strip() or "INFO").upper(),
        log_file=Path(log_file).expanduser() if log_file else None,
        cloud_default_model=env.get("KCLOUD_DEFAULT_MODEL", "").strip() or CLOUD_DEFAULT_MODEL,
        runs_dir=Path(env.get("KCLOUD_RUNS_DIR") or Path.cwd() / "runs").expanduser(),
        cloud_run_wait_seconds=_number(env, "KCLOUD_RUN_WAIT", 900, int),
        cloud_push_wait_seconds=_number(env, "KCLOUD_PUSH_WAIT", 600, int),
        kaggle_cli_timeout_seconds=_number(env, "KAGGLE_CLI_TIMEOUT", 1800.0, float),
    )
