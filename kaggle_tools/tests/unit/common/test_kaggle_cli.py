"""kaggle 子进程封装: 找不到命令、超时、失败时的友好错误。"""

import subprocess

import pytest

from kaggle_tools.common import kaggle_cli
from kaggle_tools.common.errors import KaggleCliError, KaggleCliNotFoundError


def test_missing_executable_gives_actionable_error(monkeypatch):
    monkeypatch.setattr(kaggle_cli.shutil, "which", lambda name: None)
    with pytest.raises(KaggleCliNotFoundError, match="pip install"):
        kaggle_cli.run_kaggle(["--version"])


def test_timeout_is_converted(monkeypatch):
    monkeypatch.setattr(kaggle_cli.shutil, "which", lambda name: "/bin/kaggle")

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(kaggle_cli.subprocess, "run", slow)
    with pytest.raises(KaggleCliError, match="KAGGLE_CLI_TIMEOUT"):
        kaggle_cli.run_kaggle(["b", "t", "run", "x"], timeout=5)


def test_check_raises_with_stderr(monkeypatch):
    monkeypatch.setattr(kaggle_cli.shutil, "which", lambda name: "/bin/kaggle")
    monkeypatch.setattr(
        kaggle_cli.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 2, stdout="", stderr="403 Forbidden"),
    )
    with pytest.raises(KaggleCliError, match="403 Forbidden"):
        kaggle_cli.run_kaggle(["datasets", "list"], capture=True, check=True)


def test_current_username_parses_config_view(monkeypatch):
    monkeypatch.setattr(kaggle_cli.shutil, "which", lambda name: "/bin/kaggle")
    monkeypatch.setattr(
        kaggle_cli.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="Configuration values from x\n- username: laycatbot\n", stderr=""),
    )
    assert kaggle_cli.current_username() == "laycatbot"
