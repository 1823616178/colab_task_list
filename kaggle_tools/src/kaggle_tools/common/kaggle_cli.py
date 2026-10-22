"""kaggle 命令行的统一调用入口: 带超时, 找不到命令时给出可操作的提示。"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence

from .errors import KaggleCliError, KaggleCliNotFoundError
from .logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 1800.0

NOT_FOUND_HINT = (
    "找不到 kaggle 命令。请在当前虚拟环境里安装本项目(pip install -e .), "
    "并确认 kaggle 在 PATH 上(Windows 上是 .venv\\Scripts\\kaggle.exe)。"
)


def kaggle_executable() -> str:
    """定位 kaggle 可执行文件。"""
    found = shutil.which("kaggle")
    if found is None:
        raise KaggleCliNotFoundError(NOT_FOUND_HINT)
    return found


def run_kaggle(arguments: Sequence[str], *, capture: bool = False,
               timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
               check: bool = False) -> subprocess.CompletedProcess:
    """执行 kaggle <arguments>。

    capture=False 时输出直接透传给终端; check=True 时非零退出抛 KaggleCliError。
    """
    command = [kaggle_executable(), *arguments]
    logger.debug("执行: kaggle %s (timeout=%s)", " ".join(arguments), timeout)
    try:
        completed = subprocess.run(
            command,
            capture_output=capture,
            text=True,
            encoding="utf-8" if capture else None,
            errors="replace" if capture else None,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise KaggleCliNotFoundError(NOT_FOUND_HINT) from error
    except subprocess.TimeoutExpired as error:
        raise KaggleCliError(
            f"kaggle {' '.join(arguments)} 超过 {timeout:.0f} 秒未结束, 已终止。"
            "可用 KAGGLE_CLI_TIMEOUT 调大超时。"
        ) from error
    if check and completed.returncode != 0:
        detail = ""
        if capture:
            detail = (completed.stderr or completed.stdout or "").strip()
        raise KaggleCliError(
            f"kaggle {' '.join(arguments)} 失败(exit {completed.returncode})"
            + (f": {detail}" if detail else "")
        )
    return completed


def current_username(timeout: float = 60.0) -> str:
    """从 kaggle config view 读出当前用户名; 读不到返回空串。"""
    completed = run_kaggle(["config", "view"], capture=True, timeout=timeout)
    for line in (completed.stdout or "").splitlines():
        if "username:" in line:
            return line.split(":", 1)[1].strip()
    return ""
