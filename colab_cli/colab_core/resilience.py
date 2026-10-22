"""瞬时故障分类、指数退避重试与运行期断线重连（reattach）。

- `is_transient_error`：网络抖动 / 内核连接丢失 / 网关 5xx / 超时 → 可重试；
  配置错误、会话不存在、校验失败等 → 不可重试（立即失败，不浪费计费时间）。
- `retry_transient`：对**幂等**步骤（上传、环境安装脚本、产物下载）自动退避重试。
- `reattach_workflow`：工作流执行阶段连接断开时，先探测会话是否仍存活，
  再轮询远端产物清单（remote_workflow 写完全部分块后才写 manifest.json，
  清单出现即代表远端进程已完成），就绪后改走下载通道取回结果。
  ⚠️ 执行阶段本身**不重放**：重放会让远端再跑一遍工作流（重复计费且覆盖产物）。

参数可由环境变量调整：
  COLAB_RETRY_ATTEMPTS        幂等步骤最大尝试次数（含首次），缺省 3
  COLAB_RETRY_BASE_SECONDS    首次退避秒数（指数翻倍，封顶 60s），缺省 5
  COLAB_REATTACH_TIMEOUT      执行阶段断线后等待远端完成的最长秒数，缺省 1800（0=禁用）
  COLAB_REATTACH_POLL_SECONDS 轮询远端产物清单的间隔，缺省 30
"""
import os
import re
import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

# 与 colab CLI / requests / websocket 的常见瞬时错误文案对齐（大小写不敏感）
_TRANSIENT_PATTERNS = re.compile(
    "|".join([
        r"connection (was )?(lost|reset|aborted|refused)",
        r"connection closed",
        r"broken pipe",
        r"timed? ?out",
        r"temporar(y|ily) (failure|unavailable)",
        r"\b50[234]\b",
        r"bad gateway",
        r"service unavailable",
        r"gateway time-?out",
        r"too many requests|\b429\b",
        r"max retries exceeded",
        r"name or service not known",
        r"network is unreachable",
        r"websocket.*(closed|error)",
        r"econnreset|etimedout|eai_again",
        r"remote end closed",
    ]),
    re.IGNORECASE,
)

# 明确不可重试（即使文案里也带 timeout 之类字样）
_PERMANENT_PATTERNS = re.compile(
    r"不可访问|not found|no such|permission denied|unauthori[sz]ed|forbidden|"
    r"校验|sha256|损坏|corrupt|invalid|配额|quota",
    re.IGNORECASE,
)


def is_transient_error(error: BaseException) -> bool:
    """判断异常是否为瞬时故障（值得退避重试）。"""
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    text = str(error)
    if _PERMANENT_PATTERNS.search(text):
        return False
    from colab_transport import TransportError

    if isinstance(error, (TransportError, OSError, RuntimeError)):
        return bool(_TRANSIENT_PATTERNS.search(text))
    return False


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def retry_attempts() -> int:
    return max(1, _env_int("COLAB_RETRY_ATTEMPTS", 3))


def backoff_delay(attempt: int, base: Optional[float] = None, cap: float = 60.0) -> float:
    """第 attempt 次失败后的等待秒数：base * 2^(attempt-1)，封顶 cap。"""
    base = float(_env_int("COLAB_RETRY_BASE_SECONDS", 5)) if base is None else base
    return min(base * (2 ** (attempt - 1)), cap)


def retry_transient(
    fn: Callable[[], T],
    *,
    step: str,
    on_retry: Optional[Callable[[int, int, float, BaseException], None]] = None,
    attempts: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    base_delay: Optional[float] = None,
) -> T:
    """执行幂等步骤；瞬时故障按指数退避重试，非瞬时故障立即抛出。

    on_retry(attempt, attempts, delay, error) 用于发 warn 事件（step 仅作日志标识）。
    """
    total = attempts or retry_attempts()
    for attempt in range(1, total + 1):
        try:
            return fn()
        except Exception as error:  # noqa: BLE001 - 分类后决定是否重抛
            if attempt >= total or not is_transient_error(error):
                raise
            delay = backoff_delay(attempt, base_delay)
            if on_retry is not None:
                on_retry(attempt, total, delay, error)
            sleep(delay)
    raise AssertionError(f"unreachable: {step}")  # pragma: no cover


def reattach_timeout() -> int:
    return max(0, _env_int("COLAB_REATTACH_TIMEOUT", 1800))


def reattach_workflow(
    *,
    session_alive: Callable[[], bool],
    output_ready: Callable[[], Optional[bool]],
    on_wait: Optional[Callable[[float, float], None]] = None,
    timeout: Optional[float] = None,
    poll: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """执行阶段断线后的重连：返回 True = 远端已完成、可直接下载产物。

    - 会话已不存活 → False（远端进程随 VM 消失，无从重连）；
    - output_ready() 返回 None（通道不支持探测）→ False；
    - 超时仍未就绪 → False。
    """
    limit = reattach_timeout() if timeout is None else timeout
    interval = float(_env_int("COLAB_REATTACH_POLL_SECONDS", 30)) if poll is None else poll
    if limit <= 0:
        return False
    started = clock()
    while True:
        try:
            if not session_alive():
                return False
            ready = output_ready()
        except Exception as error:  # noqa: BLE001 - 探测本身抖动：瞬时则继续等
            if not is_transient_error(error):
                return False
            ready = False
        if ready is None:
            return False
        if ready:
            return True
        elapsed = clock() - started
        if elapsed >= limit:
            return False
        if on_wait is not None:
            on_wait(elapsed, limit)
        sleep(min(interval, max(limit - elapsed, 0.0)))
