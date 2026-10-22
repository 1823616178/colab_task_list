"""网关可靠性: 429/5xx 退避重试、Retry-After、网络错误 502、非 JSON 透传、request id。"""

import httpx
from fastapi.testclient import TestClient

from kaggle_tools.common.config import Settings
from kaggle_tools.kllm.server import create_app
from tests.unit.kllm.test_server import COMPLETION_BODY, FakeTokenManager


def build(tmp_path, handler, max_retries=2, **settings_overrides):
    """注入假上游; sleep 只记录等待秒数, 不真的睡。"""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    settings = Settings(cache_dir=tmp_path, max_retries=max_retries,
                        retry_base_delay=0.5, retry_max_delay=8.0, **settings_overrides)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(settings=settings, token_manager=FakeTokenManager(),
                     http_client=client, sleep=fake_sleep)
    return TestClient(app), sleeps


def post(client, **extra):
    return client.post("/v1/chat/completions",
                       json={"model": "m", "messages": [], **extra}, headers=extra.pop("h", {}))


def test_429_is_retried_honoring_retry_after(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "slow down"})
        return httpx.Response(200, json=COMPLETION_BODY)

    client, sleeps = build(tmp_path, handler)
    response = post(client)
    assert response.status_code == 200
    assert len(calls) == 2
    assert sleeps == [3.0]


def test_5xx_retries_are_bounded_and_last_status_is_returned(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={"error": "busy"})

    client, sleeps = build(tmp_path, handler, max_retries=2)
    response = post(client)
    assert response.status_code == 503
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_network_error_becomes_502_after_retries(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("connection refused", request=request)

    client, _ = build(tmp_path, handler, max_retries=1)
    response = post(client)
    assert response.status_code == 502
    assert "ConnectError" in response.json()["detail"]
    assert len(calls) == 2


def test_transient_network_error_recovers(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=COMPLETION_BODY)

    client, _ = build(tmp_path, handler)
    assert post(client).status_code == 200


def test_read_timeout_is_not_retried_to_avoid_double_billing(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("timeout", request=request)

    client, _ = build(tmp_path, handler, max_retries=3)
    assert post(client).status_code == 502
    assert len(calls) == 1


def test_500_is_not_retried_by_default(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, json={"error": "boom"})

    client, _ = build(tmp_path, handler, max_retries=3)
    assert post(client).status_code == 500
    assert len(calls) == 1


def test_non_json_upstream_body_is_passed_through(tmp_path):
    def handler(request):
        return httpx.Response(502, content=b"<html>Bad Gateway</html>",
                              headers={"content-type": "text/html"})

    client, _ = build(tmp_path, handler, max_retries=0)
    response = post(client)
    assert response.status_code == 502
    assert response.text == "<html>Bad Gateway</html>"


def test_non_object_json_request_is_rejected(tmp_path):
    client, _ = build(tmp_path, lambda r: httpx.Response(200, json={}))
    response = client.post("/v1/chat/completions", content=b"[1, 2]")
    assert response.status_code == 400


def test_request_id_is_generated_or_echoed(tmp_path):
    client, _ = build(tmp_path, lambda r: httpx.Response(200, json=COMPLETION_BODY))
    generated = client.get("/healthz")
    assert generated.headers["X-Request-ID"]
    echoed = client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert echoed.headers["X-Request-ID"] == "abc123"
