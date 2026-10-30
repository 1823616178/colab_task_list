"""本轮优化的回归测试: 账本聚合、归档、apply 只加 diff 文件、dataset 敏感排除、
CLI 续期与额度拦截、网关总尝试上限。"""

import json
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from kaggle_tools.cloud.apply import diff_file_paths
from kaggle_tools.cloud.cli import DATASET_IGNORE_PATTERNS, find_sensitive_files
from kaggle_tools.common.config import Settings
from kaggle_tools.kllm import cli
from kaggle_tools.kllm.server import create_app
from kaggle_tools.kllm.usage import Ledger, archive_old_records
from tests.unit.kllm.test_server import FakeTokenManager


def test_ledger_model_breakdown_and_status_are_consistent(tmp_path):
    ledger = Ledger(tmp_path / "u.jsonl", timezone.utc)
    ledger.record({"ts": "2026-10-30T00:00:00+00:00", "model": "a",
                   "cost_nanodollars": 2_000_000_000, "input_tokens": 3,
                   "output_tokens": 4, "streamed": False})
    ledger.record({"ts": "2026-10-30T01:00:00+00:00", "model": "b",
                   "cost_nanodollars": 1_000_000_000, "input_tokens": 1,
                   "output_tokens": 1, "streamed": True})
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    status = ledger.status(now)
    assert status.today.cost_dollars == 3.0
    assert status.today.call_count == 2
    breakdown = ledger.model_breakdown("today", now)
    assert breakdown["a"]["cost_dollars"] == 2.0
    assert breakdown["b"]["calls"] == 1
    assert sum(item["cost_dollars"] for item in breakdown.values()) == 3.0


def test_archive_old_records_moves_only_old_months(tmp_path):
    path = tmp_path / "usage.jsonl"
    records = [
        {"ts": "2025-12-01T00:00:00+00:00", "model": "a", "cost_nanodollars": 1},
        {"ts": "2026-02-01T00:00:00+00:00", "model": "a", "cost_nanodollars": 2},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    kept, archived = archive_old_records(path, "2026-01")
    assert (kept, archived) == (1, 1)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert (tmp_path / "usage-archive.jsonl").exists()


def test_diff_file_paths_handles_new_and_deleted_files():
    text = ("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
            "--- /dev/null\n+++ b/NEW.md\n@@ -0,0 +1 @@\n+hi\n"
            "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n")
    assert diff_file_paths(text) == ("x.py", "NEW.md", "gone.py")


def test_dataset_ignore_patterns_cover_secrets():
    for pattern in (".env", ".venv", "venv", "*.pem", "*.key", ".kaggle", "runs"):
        assert pattern in DATASET_IGNORE_PATTERNS


def test_find_sensitive_files_spots_env_and_keys(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".env").write_text("K=1", encoding="utf-8")
    (tmp_path / "ok.py").write_text("x=1", encoding="utf-8")
    found = find_sensitive_files(tmp_path)
    assert len(found) == 1 and found[0].name == ".env"


def test_chat_once_with_refresh_retries_after_401(monkeypatch):
    calls = []

    def fake_chat(credentials, model, prompt, settings):
        calls.append(credentials.token)
        status = 401 if len(calls) == 1 else 200
        return httpx.Response(status, json={}, request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(cli, "chat_once", fake_chat)

    class FakeManager:
        def force_refresh(self, stale=None):
            from kaggle_tools.kllm.credentials import ProxyCredentials
            return ProxyCredentials("https://x", "new-token", None)

    response = cli.chat_once_with_refresh(
        FakeManager(), type("C", (), {"token": "old-token"})(),
        "m", "hi", Settings(cache_dir="/tmp"))
    assert response.status_code == 200
    assert calls == ["old-token", "new-token"]


def test_gateway_total_attempts_caps_retry_plus_fallback(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"error": "slow"})

    async def no_sleep(_seconds):
        return None

    settings = Settings(cache_dir=tmp_path, max_retries=3, retry_base_delay=0,
                        max_total_attempts=2, fallback_models=("backup",))
    app = create_app(settings=settings, token_manager=FakeTokenManager(),
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                     sleep=no_sleep)
    client = TestClient(app)
    response = client.post("/v1/chat/completions", json={"model": "primary", "messages": []})
    # 总尝试上限 2 次, 不会把“每模型重试满”放大成 8 次。
    assert len(calls) == 2
    assert response.status_code == 502


def test_ask_refuses_when_budget_exceeded(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KLLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("KLLM_DAILY_LIMIT", "1")
    monkeypatch.delenv("KAGGLE_TOOLS_ENV_FILE", raising=False)
    (tmp_path / "usage.jsonl").write_text(
        json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "model": "m",
                    "cost_nanodollars": 2_000_000_000}) + "\n", encoding="utf-8")

    def fail_get(self):
        raise AssertionError("超限时不应再去取凭据或发往上游")

    monkeypatch.setattr(cli.TokenManager, "get", fail_get)
    assert cli.main(["ask", "hi"]) == 1
    assert "额度上限" in capsys.readouterr().err
