"""模型代理凭据管理: 获取、缓存、过期自动续期。

Kaggle 下发的代理 token 有效期只有 1 小时, 所有请求都要先过这里拿一份活的凭据。
"""

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common.config import OPENAI_PATH_SUFFIX, Settings
from ..common.errors import CredentialError, KaggleToolsError
from ..common.kaggle_cli import run_kaggle
from ..common.logging import get_logger

logger = get_logger(__name__)

# 提前多少秒判定为过期, 留出请求本身的耗时余量。
EXPIRY_SKEW_SECONDS = 300


@dataclass(frozen=True)
class ProxyCredentials:
    """一份模型代理凭据。base_url 已经补好 /openapi 后缀。"""

    base_url: str
    token: str
    expiry: datetime | None

    def expired(self, skew_seconds: int = EXPIRY_SKEW_SECONDS) -> bool:
        """判断凭据是否已过期或即将过期。"""
        if self.expiry is None:
            return True
        remaining = (self.expiry - datetime.now(timezone.utc)).total_seconds()
        return remaining <= skew_seconds

    def to_dict(self) -> dict:
        """序列化为可写入缓存文件的字典。"""
        return {
            "base_url": self.base_url,
            "token": self.token,
            "expiry": self.expiry.isoformat() if self.expiry else None,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ProxyCredentials":
        """从缓存文件的字典还原。"""
        raw_expiry = payload.get("expiry")
        return cls(
            base_url=payload["base_url"],
            token=payload["token"],
            expiry=parse_expiry(raw_expiry) if raw_expiry else None,
        )


def parse_expiry(raw_expiry: str) -> datetime | None:
    """解析 Kaggle 返回的 ISO 过期时间, 统一成带时区的 UTC。"""
    if not raw_expiry:
        return None
    try:
        parsed = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_from_kaggle_api() -> dict[str, str]:
    """走 Kaggle CLI 的程序化入口取凭据, 不落盘。

    _fetch_model_proxy_env 是私有方法(kaggle_api_extended.py:9649), 没有稳定性承诺,
    所以调用方要准备好回落到命令行。
    """
    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    api.authenticate()
    return api._fetch_model_proxy_env("auth")


def fetch_from_kaggle_cli() -> dict[str, str]:
    """兜底方案: 调 kaggle benchmarks auth 写临时 .env 再读回来。"""
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / ".env"
        env_file.touch()
        try:
            completed = run_kaggle(
                ["benchmarks", "auth", "--no-confirm", "--env-file", str(env_file)],
                capture=True,
                timeout=120,
            )
        except KaggleToolsError as error:
            raise CredentialError(str(error)) from error
        if completed.returncode != 0:
            raise CredentialError(
                f"kaggle benchmarks auth 失败(exit {completed.returncode}): "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        values: dict[str, str] = {}
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
        return values


class TokenManager:
    """凭据的单一来源。线程安全, 并发只会触发一次刷新。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.Lock()
        self._credentials: ProxyCredentials | None = None

    def get(self) -> ProxyCredentials:
        """返回一份未过期的凭据, 必要时自动续期。"""
        cached = self._credentials or self._load_from_disk()
        if cached is not None and not cached.expired():
            self._credentials = cached
            return cached
        return self.force_refresh(stale=cached)

    def force_refresh(self, stale: ProxyCredentials | None = None) -> ProxyCredentials:
        """重新申请凭据。

        stale 是调用方手上那份已失效的凭据。拿到锁后如果当前凭据已经不是它了,
        说明别的线程刚刷过, 直接复用, 避免并发刷新风暴。
        """
        with self._lock:
            current = self._credentials
            if current is not None and current is not stale and not current.expired():
                return current
            credentials = self._fetch()
            self._credentials = credentials
            self._save_to_disk(credentials)
            return credentials

    def _fetch(self) -> ProxyCredentials:
        """依次尝试程序化入口和命令行兜底。"""
        errors: list[str] = []
        for source in (fetch_from_kaggle_api, fetch_from_kaggle_cli):
            try:
                values = source()
            except Exception as error:  # noqa: BLE001 - 任何失败都要能回落
                logger.warning("凭据来源 %s 失败: %s: %s", source.__name__,
                               type(error).__name__, error)
                errors.append(f"{source.__name__}: {type(error).__name__}: {error}")
                continue
            base_url = values.get("MODEL_PROXY_URL")
            token = values.get("MODEL_PROXY_API_KEY")
            if not base_url or not token:
                errors.append(f"{source.__name__}: 返回内容缺少 URL 或 token")
                continue
            logger.info("已通过 %s 获取新凭据", source.__name__)
            return ProxyCredentials(
                base_url=base_url.rstrip("/") + OPENAI_PATH_SUFFIX,
                token=token,
                expiry=parse_expiry(values.get("MODEL_PROXY_EXPIRY_TIME", "")),
            )
        raise CredentialError(
            "无法获取 Kaggle 模型代理凭据。请先确认 kaggle config view 能显示用户名, "
            "且账号已开通 Benchmarks。\n" + "\n".join(errors)
        )

    def _load_from_disk(self) -> ProxyCredentials | None:
        """读取磁盘缓存, 损坏就当作没有。"""
        path = self._settings.credentials_path
        if not path.exists():
            return None
        try:
            return ProxyCredentials.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError) as error:
            logger.warning("凭据缓存 %s 不可用, 将重新获取: %s", path, error)
            return None

    def _save_to_disk(self, credentials: ProxyCredentials) -> None:
        """把凭据写入缓存文件; POSIX 上权限 600, 失败只告警不影响主流程。"""
        path = self._settings.credentials_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = path.with_suffix(".json.tmp")
            temporary_path.write_text(json.dumps(credentials.to_dict()), encoding="utf-8")
            restrict_permissions(temporary_path)
            temporary_path.replace(path)
        except OSError as error:
            logger.warning("写凭据缓存 %s 失败(不影响本次使用): %s", path, error)


def restrict_permissions(path: Path) -> None:
    """把文件权限收紧到仅本人可读写。

    Windows 上 os.chmod 只能切换只读位, 设 0o600 没有意义; 用户目录本身已按账户隔离。
    """
    if os.name == "nt":
        return
    os.chmod(path, 0o600)
