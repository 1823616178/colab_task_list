"""退避策略与 Retry-After 解析。"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from kaggle_tools.common.retry import RetryPolicy, is_retryable_status, parse_retry_after


def test_retry_after_seconds():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(" 0 ") == 0.0


def test_retry_after_http_date():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    header = format_datetime(now + timedelta(seconds=30), usegmt=True)
    assert parse_retry_after(header, now=now) == 30.0


def test_retry_after_garbage_is_ignored():
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


def test_exponential_backoff_without_jitter():
    policy = RetryPolicy(max_retries=5, base_delay=1.0, max_delay=5.0, jitter=0)
    assert [policy.delay(i) for i in range(4)] == [1.0, 2.0, 4.0, 5.0]


def test_retry_after_overrides_backoff_but_is_capped():
    policy = RetryPolicy(base_delay=1.0, max_delay=10.0, jitter=0)
    assert policy.delay(0, "3") == 3.0
    assert policy.delay(0, "999") == 10.0


def test_jitter_stays_within_bounds():
    policy = RetryPolicy(base_delay=2.0, max_delay=100.0, jitter=0.5)
    for _ in range(50):
        assert 2.0 <= policy.delay(0) <= 3.0


def test_retryable_statuses():
    assert all(is_retryable_status(code) for code in (429, 500, 502, 503, 504))
    assert not any(is_retryable_status(code) for code in (200, 400, 401, 404))
