"""TokenManager 的续期、缓存与并发行为。"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from kllm import credentials as credentials_module
from kllm.config import Settings
from kllm.credentials import CredentialError, ProxyCredentials, TokenManager


def make_settings(tmp_path) -> Settings:
    """构造指向临时目录的 Settings。"""
    return Settings(
        host="127.0.0.1",
        port=8799,
        local_key=None,
        cache_dir=tmp_path,
        request_timeout_seconds=60.0,
    )


def env_payload(token: str, minutes_ahead: int) -> dict[str, str]:
    """构造一份 Kaggle 风格的凭据返回值。"""
    expiry = datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead)
    return {
        "MODEL_PROXY_URL": "https://example.invalid/models",
        "MODEL_PROXY_API_KEY": token,
        "MODEL_PROXY_EXPIRY_TIME": expiry.isoformat().replace("+00:00", "Z"),
    }


def test_base_url_gets_openapi_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_api",
                        lambda: env_payload("token-1", 60))
    manager = TokenManager(make_settings(tmp_path))
    assert manager.get().base_url == "https://example.invalid/models/openapi"


def test_fresh_credentials_are_not_refetched(tmp_path, monkeypatch):
    calls = []

    def fetch():
        calls.append(1)
        return env_payload("token-1", 60)

    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_api", fetch)
    manager = TokenManager(make_settings(tmp_path))
    manager.get()
    manager.get()
    assert len(calls) == 1


def test_expiring_credentials_are_refetched(tmp_path, monkeypatch):
    tokens = iter(["token-old", "token-new"])
    # 过期余量 1 分钟 < EXPIRY_SKEW_SECONDS(5 分钟), 应判定为需要续期。
    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_api",
                        lambda: env_payload(next(tokens), 1))
    manager = TokenManager(make_settings(tmp_path))
    assert manager.get().token == "token-old"
    assert manager.get().token == "token-new"


def test_force_refresh_replaces_a_stale_credential(tmp_path, monkeypatch):
    tokens = iter(["token-old", "token-new"])
    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_api",
                        lambda: env_payload(next(tokens), 60))
    manager = TokenManager(make_settings(tmp_path))
    stale = manager.get()
    # 凭据时间上没过期, 但上游拒绝了它, 必须真的换一份。
    assert manager.force_refresh(stale=stale).token == "token-new"


def test_concurrent_refresh_fetches_once(tmp_path, monkeypatch):
    calls = []
    barrier = threading.Barrier(10)

    def fetch():
        calls.append(1)
        return env_payload("token-1", 60)

    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_api", fetch)
    manager = TokenManager(make_settings(tmp_path))

    def worker():
        barrier.wait()
        manager.get()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1


def test_disk_cache_survives_a_new_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_api",
                        lambda: env_payload("token-1", 60))
    settings = make_settings(tmp_path)
    TokenManager(settings).get()

    def explode():
        raise AssertionError("不应该再次向 Kaggle 请求")

    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_api", explode)
    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_cli", explode)
    assert TokenManager(settings).get().token == "token-1"
    assert oct(settings.credentials_path.stat().st_mode)[-3:] == "600"


def test_falls_back_to_cli_when_private_api_breaks(tmp_path, monkeypatch):
    def broken():
        raise AttributeError("_fetch_model_proxy_env 没了")

    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_api", broken)
    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_cli",
                        lambda: env_payload("token-cli", 60))
    assert TokenManager(make_settings(tmp_path)).get().token == "token-cli"


def test_both_sources_failing_raises_credential_error(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_api", broken)
    monkeypatch.setattr(credentials_module, "fetch_from_kaggle_cli", broken)
    with pytest.raises(CredentialError):
        TokenManager(make_settings(tmp_path)).get()


def test_expired_helper_treats_missing_expiry_as_expired():
    assert ProxyCredentials("https://x/openapi", "t", None).expired()
