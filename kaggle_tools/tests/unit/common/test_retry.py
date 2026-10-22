"""退避策略与 Retry-After 解析。"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

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


def test_default_retryable_statuses_avoid_double_billing():
    assert is_retryable_status(429) and is_retryable_status(503)
    assert not any(is_retryable_status(code) for code in (200, 400, 401, 404, 500, 502, 504))


def test_500_502_can_be_enabled_explicitly():
    policy = RetryPolicy(retry_statuses=frozenset({429, 500, 502, 503}))
    assert policy.should_retry_status(500) and policy.should_retry_status(502)


def test_only_unsent_request_errors_are_retried():
    request = httpx.Request("POST", "https://x.invalid")
    assert RetryPolicy.should_retry_error(httpx.ConnectError("x", request=request))
    assert RetryPolicy.should_retry_error(httpx.ConnectTimeout("x", request=request))
    assert not RetryPolicy.should_retry_error(httpx.ReadTimeout("x", request=request))
    assert not RetryPolicy.should_retry_error(httpx.RemoteProtocolError("x", request=request))
