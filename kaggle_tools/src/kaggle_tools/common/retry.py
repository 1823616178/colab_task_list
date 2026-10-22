"""重试策略: 指数退避 + 抖动, 并遵守上游的 Retry-After。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# 值得重试的上游状态码: 限流与临时性网关错误。
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """max_retries 是失败后再试的次数(总尝试数 = max_retries + 1)。"""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.1

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


def is_retryable_status(status_code: int) -> bool:
    """该状态码是否应该重试。"""
    return status_code in RETRYABLE_STATUSES
