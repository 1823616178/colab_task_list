"""网关功能: 额度上限/告警、/v1/usage、并发上限、备用模型、流式中断。"""

import asyncio
import json
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from kaggle_tools.common.config import Settings
from kaggle_tools.kllm.server import create_app
from kaggle_tools.kllm.usage import Ledger, load_records
from tests.unit.kllm.test_server import COMPLETION_BODY, FakeTokenManager

DOLLAR = 1_000_000_000


async def no_sleep(_seconds: float) -> None:
    return None


def build(tmp_path, handler, **overrides):
    settings = Settings(cache_dir=tmp_path, max_retries=1, retry_base_delay=0,
                        **overrides)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(settings=settings, token_manager=FakeTokenManager(),
                     http_client=client, sleep=no_sleep)
    return TestClient(app), settings


def spend(settings, dollars: float, when: datetime | None = None):
    when = when or datetime.now(timezone.utc)
    record = {"ts": when.isoformat(), "model": "m", "streamed": False,
              "input_tokens": 1, "output_tokens": 1, "cost_nanodollars": int(dollars * DOLLAR)}
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    with settings.usage_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def chat(client, **extra):
    return client.post("/v1/chat/completions", json={"model": "m", "messages": [], **extra})


def ok_handler(request):
    return httpx.Response(200, json=COMPLETION_BODY)


# ==== 额度 ===================================================================


def test_request_is_rejected_when_daily_limit_reached(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return ok_handler(request)

    settings_probe = Settings(cache_dir=tmp_path)
    spend(settings_probe, 10.0)
    client, _ = build(tmp_path, handler, daily_limit_dollars=10.0)
    response = chat(client)
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "budget_exceeded"
    assert calls == []


def test_monthly_limit_also_enforced(tmp_path):
    spend(Settings(cache_dir=tmp_path), 50.0)
    client, _ = build(tmp_path, ok_handler, daily_limit_dollars=0, monthly_limit_dollars=40.0)
    assert chat(client).status_code == 429


def test_zero_limit_means_unlimited(tmp_path):
    spend(Settings(cache_dir=tmp_path), 1000.0)
    client, _ = build(tmp_path, ok_handler, daily_limit_dollars=0, monthly_limit_dollars=0)
    assert chat(client).status_code == 200


def test_warning_header_near_the_limit(tmp_path):
    spend(Settings(cache_dir=tmp_path), 8.5)
    client, _ = build(tmp_path, ok_handler, daily_limit_dollars=10.0)
    response = chat(client)
    assert response.status_code == 200
    assert response.headers["X-KLLM-Budget-Warning"].startswith("daily=85%")
    assert response.headers["X-KLLM-Budget"].startswith("today=8.5000/10.00")


def test_no_warning_when_far_from_limit(tmp_path):
    client, _ = build(tmp_path, ok_handler)
    assert "X-KLLM-Budget-Warning" not in chat(client).headers


def test_usage_endpoint_reports_totals(tmp_path):
    spend(Settings(cache_dir=tmp_path), 2.0)
    client, _ = build(tmp_path, ok_handler)
    chat(client)  # COMPLETION_BODY 记一笔 0.2065 美元
    body = client.get("/v1/usage").json()
    assert body["timezone"] == "Asia/Shanghai"
    assert body["today"]["calls"] == 2
    assert abs(body["today"]["cost_dollars"] - 2.0002065) < 1e-6
    assert body["today"]["limit_dollars"] == 10.0


def test_day_boundary_follows_configured_timezone(tmp_path):
    ledger = Ledger(tmp_path / "u.jsonl", __import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
    # UTC 2026-10-21 17:00 = 上海 2026-10-22 01:00
    ledger.record({"ts": "2026-10-21T17:00:00+00:00", "cost_nanodollars": DOLLAR})
    now = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)
    status = ledger.status(now)
    assert status.day_key == "2026-10-22"
    assert status.today.cost_dollars == 1.0
    utc = Ledger(tmp_path / "u.jsonl", timezone.utc).status(now)
    assert utc.today.cost_dollars == 0.0


# ==== 并发 ===================================================================


def test_concurrency_is_capped(tmp_path):
    state = {"active": 0, "peak": 0}

    async def handler(request):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return httpx.Response(200, json=COMPLETION_BODY)

    settings = Settings(cache_dir=tmp_path, max_concurrency=2)
    app = create_app(settings=settings, token_manager=FakeTokenManager(),
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                     sleep=no_sleep)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            results = await asyncio.gather(*[
                client.post("/v1/chat/completions", json={"model": "m", "messages": []})
                for _ in range(6)
            ])
        return [r.status_code for r in results]

    assert asyncio.run(run()) == [200] * 6
    assert state["peak"] == 2


# ==== 备用模型 ===============================================================


def test_fallback_model_is_used_when_primary_fails(tmp_path):
    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "primary":
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json=COMPLETION_BODY)

    client, settings = build(tmp_path, handler, fallback_models=("backup",))
    response = chat(client, model="primary")
    assert response.status_code == 200
    assert response.headers["X-KLLM-Model"] == "backup"
    assert seen == ["primary", "backup"]
    assert load_records(settings.usage_path)[-1]["model"] == "backup"


def test_fallback_on_network_error(tmp_path):
    def handler(request):
        if json.loads(request.content)["model"] == "primary":
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=COMPLETION_BODY)

    client, _ = build(tmp_path, handler, fallback_models=("backup",))
    assert chat(client, model="primary").headers["X-KLLM-Model"] == "backup"


def test_client_errors_do_not_trigger_fallback(tmp_path):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(400, json={"error": "bad request"})

    client, _ = build(tmp_path, handler, fallback_models=("backup",))
    assert chat(client, model="primary").status_code == 400
    assert seen == ["primary"]


def test_last_failure_is_returned_when_all_models_fail(tmp_path):
    client, _ = build(tmp_path, lambda r: httpx.Response(500, json={"error": "x"}),
                      fallback_models=("b1", "b2"))
    response = chat(client, model="primary")
    assert response.status_code == 500
    assert response.headers["X-KLLM-Model"] == "b2"


# ==== 流式中断 ===============================================================


class BrokenStream(httpx.AsyncByteStream):
    """先吐一段正常数据, 然后网络断开。"""

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"he"}}],"usage":{"prompt_tokens":3}}\n\n'
        raise httpx.ReadError("connection reset")


def test_stream_interruption_sends_error_event_and_done(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=BrokenStream())

    client, settings = build(tmp_path, handler)
    response = chat(client, stream=True)
    body = response.text
    assert '"content":"he"' in body
    assert "upstream_stream_interrupted" in body
    assert body.rstrip().endswith("data: [DONE]")
    record = load_records(settings.usage_path)[-1]
    assert record["incomplete"] is True and record["streamed"] is True
    assert record["input_tokens"] == 3


def test_complete_stream_is_not_marked_incomplete(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: {"choices":[]}\n\ndata: [DONE]\n\n')

    client, settings = build(tmp_path, handler)
    assert "upstream_stream_interrupted" not in chat(client, stream=True).text
    assert "incomplete" not in load_records(settings.usage_path)[-1]
