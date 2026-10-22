"""网关转发行为: 透传、401 续期重试、流式保真、本地鉴权、记账。"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from kaggle_tools.kllm.config import Settings
from kaggle_tools.kllm.credentials import ProxyCredentials
from kaggle_tools.kllm.server import create_app
from kaggle_tools.kllm.usage import load_records

COMPLETION_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
    "usage": {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "cost": {
            "input_tokens_cost_nanodollars": 2500,
            "output_tokens_cost_nanodollars": 204000,
        },
    },
}

SSE_BYTES = (
    b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
    b"data: [DONE]\n\n"
)


class FakeTokenManager:
    """按调用顺序吐出不同 token 的假凭据源, 并记录刷新次数。"""

    def __init__(self, tokens=("token-1", "token-2")):
        self._tokens = list(tokens)
        self._index = 0
        self.refresh_count = 0

    def _current(self) -> ProxyCredentials:
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        return ProxyCredentials("https://upstream.invalid/models/openapi",
                                self._tokens[self._index], expiry)

    def get(self) -> ProxyCredentials:
        return self._current()

    def force_refresh(self, stale=None) -> ProxyCredentials:
        self.refresh_count += 1
        self._index = min(self._index + 1, len(self._tokens) - 1)
        return self._current()


def build(tmp_path, handler, local_key=None, tokens=("token-1", "token-2")):
    """组装一个注入了假上游和假凭据源的测试客户端。"""
    settings = Settings(
        host="127.0.0.1",
        port=8799,
        local_key=local_key,
        cache_dir=tmp_path,
        request_timeout_seconds=30.0,
    )
    manager = FakeTokenManager(tokens)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(settings=settings, token_manager=manager, http_client=client)
    return TestClient(app), settings, manager


def test_non_streaming_request_is_forwarded_verbatim(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=COMPLETION_BODY)

    client, _, _ = build(tmp_path, handler)
    payload = {"model": "google/gemini-3-flash-preview",
               "messages": [{"role": "user", "content": "hi"}],
               "temperature": 0.3}
    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.json() == COMPLETION_BODY
    assert seen["url"] == "https://upstream.invalid/models/openapi/chat/completions"
    assert seen["auth"] == "Bearer token-1"
    assert seen["body"] == payload


def test_upstream_401_triggers_one_refresh_and_retry(tmp_path):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["authorization"])
        if len(attempts) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json=COMPLETION_BODY)

    client, _, manager = build(tmp_path, handler)
    response = client.post("/v1/chat/completions",
                           json={"model": "m", "messages": []})

    assert response.status_code == 200
    assert attempts == ["Bearer token-1", "Bearer token-2"]
    assert manager.refresh_count == 1


def test_second_401_is_returned_without_further_retry(tmp_path):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"error": "nope"})

    client, _, manager = build(tmp_path, handler)
    response = client.post("/v1/chat/completions",
                           json={"model": "m", "messages": []})

    assert response.status_code == 401
    assert len(attempts) == 2
    assert manager.refresh_count == 1


def test_streaming_bytes_pass_through_unchanged(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=SSE_BYTES,
                              headers={"content-type": "text/event-stream"})

    client, _, _ = build(tmp_path, handler)
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "m", "messages": [], "stream": True}) as response:
        assert response.status_code == 200
        assert b"".join(response.iter_bytes()) == SSE_BYTES


def test_upstream_error_body_is_preserved(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad model"}})

    client, _, _ = build(tmp_path, handler)
    response = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "bad model"


def test_usage_is_recorded_for_non_streaming_calls(tmp_path):
    client, settings, _ = build(tmp_path, lambda r: httpx.Response(200, json=COMPLETION_BODY))
    client.post("/v1/chat/completions",
                json={"model": "google/gemini-3-flash-preview", "messages": []})

    records = load_records(settings.usage_path)
    assert len(records) == 1
    assert records[0]["model"] == "google/gemini-3-flash-preview"
    assert records[0]["input_tokens"] == 5
    assert records[0]["output_tokens"] == 7
    assert records[0]["cost_nanodollars"] == 206500
    assert records[0]["streamed"] is False


def test_streaming_without_usage_records_zero_cost(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_BYTES,
                              headers={"content-type": "text/event-stream"})

    client, settings, _ = build(tmp_path, handler)
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "m", "messages": [], "stream": True}) as response:
        response.read()

    records = load_records(settings.usage_path)
    assert len(records) == 1
    assert records[0]["streamed"] is True
    assert records[0]["cost_nanodollars"] == 0


def test_local_key_is_enforced_when_configured(tmp_path):
    handler = lambda request: httpx.Response(200, json=COMPLETION_BODY)  # noqa: E731
    client, _, _ = build(tmp_path, handler, local_key="secret")

    assert client.post("/v1/chat/completions", json={"model": "m", "messages": []}).status_code == 401
    assert client.post("/v1/chat/completions", json={"model": "m", "messages": []},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/v1/chat/completions", json={"model": "m", "messages": []},
                       headers={"Authorization": "Bearer secret"}).status_code == 200


def test_models_endpoint_lists_the_static_catalog(tmp_path):
    client, _, _ = build(tmp_path, lambda r: httpx.Response(500))
    body = client.get("/v1/models").json()
    ids = [entry["id"] for entry in body["data"]]
    assert "anthropic/claude-sonnet-5@default" in ids
    assert body["object"] == "list"


def test_malformed_json_body_is_rejected(tmp_path):
    client, _, _ = build(tmp_path, lambda r: httpx.Response(200, json=COMPLETION_BODY))
    response = client.post("/v1/chat/completions", content=b"not json",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400


SSE_WITH_USAGE = (
    b'data: {"choices":[{"delta":{"content":"hi"}}],'
    b'"usage":{"prompt_tokens":1,"completion_tokens":1,'
    b'"cost":{"input_tokens_cost_nanodollars":10,"output_tokens_cost_nanodollars":20}}}\n\n'
    b'data: {"choices":[{"delta":{}}],'
    b'"usage":{"prompt_tokens":14,"completion_tokens":9,'
    b'"cost":{"input_tokens_cost_nanodollars":28000,"output_tokens_cost_nanodollars":90000}}}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.mark.parametrize("chunk_size", [7, 64, 4096])
def test_streaming_usage_is_taken_from_the_last_chunk(tmp_path, chunk_size):
    """分块边界不能影响用量抽取, 所以按不同粒度切分重放。"""

    async def stream_body():
        for start in range(0, len(SSE_WITH_USAGE), chunk_size):
            yield SSE_WITH_USAGE[start:start + chunk_size]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream_body(),
                              headers={"content-type": "text/event-stream"})

    client, settings, _ = build(tmp_path, handler)
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "m", "messages": [], "stream": True}) as response:
        assert response.read() == SSE_WITH_USAGE

    record = load_records(settings.usage_path)[0]
    assert record["streamed"] is True
    assert record["input_tokens"] == 14
    assert record["output_tokens"] == 9
    assert record["cost_nanodollars"] == 118000
