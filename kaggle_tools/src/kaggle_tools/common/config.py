"""集中配置: 默认值、模型列表、预算都只在这里定义一次。

读取顺序: 真实环境变量 > 当前目录 .env(或 KAGGLE_TOOLS_ENV_FILE 指定的文件) > 默认值。
所有数值都会校验, 非法值抛 ConfigError 并指出是哪个变量。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

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
    # 额度控制: 达到 warn_ratio 告警, 达到上限返回 429; 上限 <= 0 表示不限制。
    daily_limit_dollars: float = DAILY_BUDGET_DOLLARS
    monthly_limit_dollars: float = MONTHLY_BUDGET_DOLLARS
    budget_warn_ratio: float = 0.8
    # 用量按哪个时区切分"今天/本月"。
    usage_timezone: str = "Asia/Shanghai"
    # 同时转发给上游的请求数上限, 多出的排队等待。
    max_concurrency: int = 4
    # 默认只重试不会重复计费的状态; 500/502 可能上游已处理, 需显式加入。
    retry_statuses: tuple[int, ...] = (429, 503)
    # 首选模型失败(429/5xx/网络)时依次尝试的备用模型。
    fallback_models: tuple[str, ...] = ()
    # 排队等待上游并发槽位的最长秒数, 0 表示无限等待(不推荐)。
    queue_timeout_seconds: float = 30.0
    # 一次网关请求在“重试 + 换备用模型”全链路上的总尝试上限, 0 表示不限制。
    # 默认 8: 首选模型重试 3 次(共 4 次)后, 备用模型链路最多再试 4 次,
    # 避免“每模型都重试满”把延迟和费用放大数倍。
    max_total_attempts: int = 8
    # 单个网关请求体的最大字节数, 超出返回 413, 防止超大请求占满内存。
    max_request_bytes: int = 10_000_000
    # kcloud local 本机执行的总超时(秒), 0 表示不限制; Modal 执行超时见下。
    local_timeout_seconds: int = 3600
    modal_timeout_seconds: int = 3600
    # 实际生效的 .env 文件(如果有), 仅用于启动时提示, 不参与校验。
    env_file: Path | None = None

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
        if not 0 < self.budget_warn_ratio <= 1:
            raise ConfigError(f"KLLM_BUDGET_WARN_RATIO 须在 (0, 1] 之间, 收到 {self.budget_warn_ratio}")
        if self.max_concurrency < 1:
            raise ConfigError(f"KLLM_MAX_CONCURRENCY 至少为 1, 收到 {self.max_concurrency}")
        if self.queue_timeout_seconds < 0:
            raise ConfigError(f"KLLM_QUEUE_TIMEOUT 不能为负数, 收到 {self.queue_timeout_seconds}")
        if self.max_total_attempts < 0:
            raise ConfigError(f"KLLM_MAX_TOTAL_ATTEMPTS 不能为负数, 收到 {self.max_total_attempts}")
        if self.max_request_bytes <= 0:
            raise ConfigError(f"KLLM_MAX_REQUEST_BYTES 必须大于 0, 收到 {self.max_request_bytes}")
        if self.local_timeout_seconds < 0 or self.modal_timeout_seconds < 0:
            raise ConfigError("KCLOUD_LOCAL_TIMEOUT / KCLOUD_MODAL_TIMEOUT 不能为负数")
        for code in self.retry_statuses:
            if not 400 <= code <= 599:
                raise ConfigError(f"KLLM_RETRY_STATUSES 只能包含 4xx/5xx, 收到 {code}")
        try:
            self.tzinfo  # noqa: B018 - 校验时区名
        except Exception as error:  # noqa: BLE001
            raise ConfigError(f"KLLM_TIMEZONE 不是有效时区: {self.usage_timezone!r}") from error

    @property
    def tzinfo(self) -> tzinfo:
        """用量切分所用的时区。"""
        return ZoneInfo(self.usage_timezone)

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


def _csv(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in environ.get(name, "").split(",") if item.strip())


def _statuses(environ: Mapping[str, str], default: tuple[int, ...]) -> tuple[int, ...]:
    if "KLLM_RETRY_STATUSES" not in environ:
        return default
    try:
        return tuple(int(item) for item in _csv(environ, "KLLM_RETRY_STATUSES"))
    except ValueError:
        raise ConfigError(
            f"KLLM_RETRY_STATUSES 应为逗号分隔的状态码, 收到 {environ['KLLM_RETRY_STATUSES']!r}"
        ) from None


def load_settings(environ: Mapping[str, str] | None = None,
                  env_file: Path | None = None) -> Settings:
    """从环境变量(及 .env)构造并校验 Settings。"""
    env = merged_environment(environ, env_file)
    # 记录实际生效的 .env, 便于启动时告诉用户“配置到底从哪读的”:
    # 显式传入的文件优先, 其次 KAGGLE_TOOLS_ENV_FILE, 最后当前目录的 .env。
    if env_file is not None:
        effective_env_file = env_file if env_file.is_file() else None
    else:
        configured = env.get("KAGGLE_TOOLS_ENV_FILE", "").strip()
        candidate = Path(configured).expanduser() if configured else Path.cwd() / ".env"
        effective_env_file = candidate if candidate.is_file() else None
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
        daily_limit_dollars=_number(env, "KLLM_DAILY_LIMIT", DAILY_BUDGET_DOLLARS, float),
        monthly_limit_dollars=_number(env, "KLLM_MONTHLY_LIMIT", MONTHLY_BUDGET_DOLLARS, float),
        budget_warn_ratio=_number(env, "KLLM_BUDGET_WARN_RATIO", 0.8, float),
        usage_timezone=env.get("KLLM_TIMEZONE", "").strip() or "Asia/Shanghai",
        max_concurrency=_number(env, "KLLM_MAX_CONCURRENCY", 4, int),
        retry_statuses=_statuses(env, (429, 503)),
        fallback_models=_csv(env, "KLLM_FALLBACK_MODELS"),
        queue_timeout_seconds=_number(env, "KLLM_QUEUE_TIMEOUT", 30.0, float),
        max_total_attempts=_number(env, "KLLM_MAX_TOTAL_ATTEMPTS", 8, int),
        max_request_bytes=_number(env, "KLLM_MAX_REQUEST_BYTES", 10_000_000, int),
        local_timeout_seconds=_number(env, "KCLOUD_LOCAL_TIMEOUT", 3600, int),
        modal_timeout_seconds=_number(env, "KCLOUD_MODAL_TIMEOUT", 3600, int),
        env_file=effective_env_file,
    )
