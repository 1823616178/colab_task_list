"""重试策略: 指数退避 + 抖动, 并遵守上游的 Retry-After。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

# 默认只重试"上游肯定没处理"的状态: 限流和服务不可用。
# 500/502/504 时上游可能已经处理并计费, 重试会重复扣额度, 需通过配置显式开启。
DEFAULT_RETRY_STATUSES = frozenset({429, 503})
# 请求还没送达上游的网络错误, 重试不会重复计费。
SAFE_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


@dataclass(frozen=True)
class RetryPolicy:
    """max_retries 是失败后再试的次数(总尝试数 = max_retries + 1)。"""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.1
    retry_statuses: frozenset[int] = DEFAULT_RETRY_STATUSES

    def should_retry_status(self, status_code: int) -> bool:
        """该状态码在本策略下是否重试。"""
        return status_code in self.retry_statuses

    @staticmethod
    def should_retry_error(error: Exception) -> bool:
        """只重试请求未送达上游的网络错误; 读超时等可能已计费, 不重试。"""
        return isinstance(error, SAFE_TRANSPORT_ERRORS)

    def delay(self, attempt: int, retry_after: str | None = None) -> float:
        """第 attempt 次重试(从 0 开始)前应等待的秒数。"""
        hinted = parse_retry_after(retry_after)
        if hinted is not None:
            return min(hinted, self.max_delay)
        backoff = min(self.base_delay * (2 ** attempt), self.max_delay)
        if self.jitter:
            backoff += random.uniform(0, backoff * self.jitter)
        return min(backoff, self.max_delay)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """解析 Retry-After: 秒数或 HTTP 日期; 无法解析返回 None。"""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (moment - now).total_seconds())


def is_retryable_status(status_code: int,
                        statuses: frozenset[int] = DEFAULT_RETRY_STATUSES) -> bool:
    """该状态码是否应该重试。"""
    return status_code in statuses


def policy_from_settings(settings) -> RetryPolicy:
    """按 Settings 构造重试策略。"""
    return RetryPolicy(
        max_retries=settings.max_retries,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
        retry_statuses=frozenset(settings.retry_statuses),
    )
