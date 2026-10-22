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
