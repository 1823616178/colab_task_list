"""kllm 命令行: 同步重试与错误处理。"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kaggle_tools.common.config import Settings
from kaggle_tools.common.errors import KaggleToolsError
from kaggle_tools.kllm import cli
from kaggle_tools.kllm.credentials import ProxyCredentials

CREDENTIALS = ProxyCredentials("https://upstream.invalid/openapi", "t",
                               datetime.now(timezone.utc) + timedelta(hours=1))


def test_chat_once_retries_429_then_succeeds(tmp_path):
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"choices": []})

    response = cli.chat_once(CREDENTIALS, "m", "hi", Settings(cache_dir=tmp_path),
                             transport=httpx.MockTransport(handler), sleep=sleeps.append)
    assert response.status_code == 200
    assert sleeps == [2.0]


def test_chat_once_raises_friendly_error_on_network_failure(tmp_path):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(KaggleToolsError, match="无法连接"):
        cli.chat_once(CREDENTIALS, "m", "hi", Settings(cache_dir=tmp_path, max_retries=1),
                      transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_models_marks_default(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("KLLM_CACHE_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAGGLE_TOOLS_ENV_FILE", raising=False)
    assert cli.main(["models"]) == 0
    assert "(默认)" in capsys.readouterr().out


def test_bad_config_is_reported_without_traceback(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAGGLE_TOOLS_ENV_FILE", raising=False)
    monkeypatch.setenv("KLLM_PORT", "nope")
    assert cli.main(["models"]) == 1
    assert "KLLM_PORT" in capsys.readouterr().err


@pytest.mark.parametrize("host, expected", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True),
    ("0.0.0.0", False), ("192.168.1.5", False), ("my-host", False),
])
def test_is_loopback(host, expected):
    assert cli.is_loopback(host) is expected


def test_serve_refuses_public_bind_without_key(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAGGLE_TOOLS_ENV_FILE", raising=False)
    monkeypatch.delenv("KLLM_LOCAL_KEY", raising=False)
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert "KLLM_LOCAL_KEY" in capsys.readouterr().err


def test_serve_allows_public_bind_with_insecure_flag(monkeypatch, tmp_path):
    import uvicorn

    started = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KLLM_LOCAL_KEY", raising=False)
    monkeypatch.setenv("KLLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(kw))
    assert cli.main(["serve", "--host", "0.0.0.0", "--insecure"]) == 0
    assert started["host"] == "0.0.0.0"


def test_serve_allows_public_bind_with_key(monkeypatch, tmp_path):
    import uvicorn

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLLM_LOCAL_KEY", "secret")
    monkeypatch.setenv("KLLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 0


def test_models_check_reports_each_model(capsys, monkeypatch, tmp_path):
    from kaggle_tools.common.config import AVAILABLE_MODELS

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.TokenManager, "get", lambda self: CREDENTIALS)
    broken = AVAILABLE_MODELS[0]

    def fake_chat(credentials, model, prompt, settings):
        status = 503 if model == broken else 200
        return httpx.Response(status, json={}, request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(cli, "chat_once", fake_chat)
    assert cli.main(["models", "--check"]) == 1
    out = capsys.readouterr().out
    assert f"❌ {broken}" in out
    assert f"{len(AVAILABLE_MODELS) - 1}/{len(AVAILABLE_MODELS)} 个模型可用" in out


def test_chat_once_does_not_retry_500_by_default(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    response = cli.chat_once(CREDENTIALS, "m", "hi", Settings(cache_dir=tmp_path),
                             transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert response.status_code == 500 and len(calls) == 1
