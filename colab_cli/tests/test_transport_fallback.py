"""通道降级回归：私有 API 探测失败时确实退回 CliTransport。"""

import importlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import colab_transport
from colab_transport import CliTransport, NativeTransport, TransportError, get_transport
from colab_transport.cli import (
    ColabCli,
    cancel_drive_mount,
    confirm_drive_mount,
    resolve_config_path,
)


def test_kernel_probe_checks_exported_client_without_connecting(monkeypatch):
    """A ColabRuntime property alone cannot prove that its dependency is usable."""
    importlib.import_module("colab_cli.runtime")

    class OfflineClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Dependency probes must not connect to a kernel")

        def start(self):
            pass

        def stop(self):
            pass

        def execute(self):
            pass

        def execute_interactive(self):
            pass

    module = SimpleNamespace(
        KernelClient=OfflineClient,
        JupyterSubprotocol=SimpleNamespace(DEFAULT=""),
    )
    monkeypatch.setitem(sys.modules, "jupyter_kernel_client", module)
    assert colab_transport.probe_kernel_client_api() is True
    del module.KernelClient
    assert colab_transport.probe_kernel_client_api() is False
    assert colab_transport.probe_private_api()["kernel_client"] is False
    module.KernelClient = OfflineClient
    del module.JupyterSubprotocol
    assert colab_transport.probe_kernel_client_api() is False


@pytest.mark.parametrize("mode", ["native", "cli"])
def test_incompatible_kernel_dependency_blocks_both_transports(monkeypatch, mode):
    monkeypatch.setattr(colab_transport, "probe_kernel_client_api", lambda: False)
    with pytest.raises(TransportError, match="内核依赖不兼容"):
        get_transport(mode)


def test_installed_client_supports_actual_colab_runtime_construction(monkeypatch):
    """Exercise CLI 0.6.0's real client lookup and constructor, with networking disabled."""
    import jupyter_kernel_client
    import requests
    from colab_cli.runtime import ColabRuntime

    def reject_network(*args, **kwargs):
        raise AssertionError("This compatibility test must not contact Colab")

    monkeypatch.setattr(requests.Session, "request", reject_network)
    monkeypatch.setattr(jupyter_kernel_client.KernelClient, "start", lambda self: None)
    monkeypatch.setattr(ColabRuntime, "_apply_ws_hook", lambda self: None)
    runtime = ColabRuntime(
        url="http://127.0.0.1:1", token="test", session_id="offline-session",
    )
    client = runtime.kernel_client
    assert isinstance(client, jupyter_kernel_client.KernelClient)
    assert client._own_kernel is False
    assert callable(client.execute_interactive)
    client.stop()


def test_native_when_probe_ok(monkeypatch):
    monkeypatch.setattr(
        colab_transport,
        "probe_private_api",
        lambda: {"contents_client": True, "kernel_client": True},
    )
    transport = get_transport("native")
    assert isinstance(transport, NativeTransport)
    assert transport.mode == "native"


def test_fallback_to_cli_when_probe_fails(monkeypatch):
    monkeypatch.setattr(
        colab_transport,
        "probe_private_api",
        lambda: {"contents_client": False, "kernel_client": True},
    )
    warnings = []
    transport = get_transport(
        "native",
        sink=lambda phase, status, message, data=None: warnings.append(
            (phase, status, message, data)
        ),
    )
    assert isinstance(transport, CliTransport)
    assert transport.mode == "cli"
    assert len(warnings) == 1
    phase, status, message, data = warnings[0]
    assert status == "warn"
    assert "降级" in message
    assert data["reason"] == "contents_api_probe_failed"
    assert data["fallback"] == "cli"


def test_cli_mode_skips_probe(monkeypatch):
    def forbidden_probe():
        raise AssertionError("cli 模式不应触发私有 API 探测")

    monkeypatch.setattr(colab_transport, "probe_private_api", forbidden_probe)
    assert isinstance(get_transport("cli"), CliTransport)


def test_env_var_selects_mode(monkeypatch):
    monkeypatch.setenv("COLAB_TRANSPORT", "cli")
    monkeypatch.setattr(
        colab_transport,
        "probe_private_api",
        lambda: {"contents_client": True, "kernel_client": True},
    )
    assert isinstance(get_transport(), CliTransport)
    monkeypatch.setenv("COLAB_TRANSPORT", "native")
    assert isinstance(get_transport(), NativeTransport)


def test_unknown_mode_rejected(monkeypatch):
    import pytest

    with pytest.raises(ValueError):
        get_transport("bogus")


def test_per_session_config_path(tmp_path, monkeypatch):
    """COLAB_CLI_STATE_DIR → 每 session 一个状态文件（文件名消毒）。"""
    monkeypatch.setenv("COLAB_CLI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLAB_CONFIG", raising=False)
    path_a = resolve_config_path("comfyui-h3")
    path_b = resolve_config_path("dev session/2")
    assert path_a == tmp_path / "comfyui-h3.json"
    assert path_b.parent == tmp_path
    assert "/" not in path_b.name and " " not in path_b.name
    assert path_a != path_b


def test_single_config_override(tmp_path, monkeypatch):
    monkeypatch.delenv("COLAB_CLI_STATE_DIR", raising=False)
    monkeypatch.setenv("COLAB_CONFIG", str(tmp_path / "dev.json"))
    assert resolve_config_path("a") == tmp_path / "dev.json"
    assert resolve_config_path("b") == tmp_path / "dev.json"


def test_default_config_is_none(monkeypatch):
    monkeypatch.delenv("COLAB_CLI_STATE_DIR", raising=False)
    monkeypatch.delenv("COLAB_CONFIG", raising=False)
    assert resolve_config_path("comfyui-h3") is None


def test_drive_mount_streams_auth_url_and_accepts_confirmation(monkeypatch):
    """后台 Runner 无 TTY 时也能展示 OAuth URL，并由确认接口发送 Enter。"""
    auth_url = "https://accounts.google.com/o/oauth2/auth?state=test-state"

    def fake_build(_self, *_args):
        code = (
            f"print({auth_url!r}, flush=True); "
            "open('/dev/tty').readline(); "
            "print('drive mounted', flush=True)"
        )
        return [sys.executable, "-c", code]

    monkeypatch.setattr(ColabCli, "build", fake_build)
    emitted = []
    transport = CliTransport(
        lambda phase, status, message, data=None: emitted.append(
            (phase, status, message, data or {})
        )
    )
    result = []
    worker = threading.Thread(
        target=lambda: result.append(transport.mount_drive("drive-test"))
    )
    worker.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not any(
        event[3].get("authorization_url") == auth_url for event in emitted
    ):
        time.sleep(0.01)

    assert any(event[0] == "log.line" and auth_url in event[2]
               for event in emitted)
    auth_event = next(
        event for event in emitted
        if event[3].get("authorization_url") == auth_url
    )
    assert auth_event[0:2] == ("drive.mount", "start")
    assert auth_event[3]["confirmation_required"] is True
    assert confirm_drive_mount("drive-test") is True

    worker.join(timeout=3)
    assert not worker.is_alive()
    assert result == [True]
    assert confirm_drive_mount("drive-test") is False


def test_drive_mount_nonzero_is_nonfatal_warning(monkeypatch):
    def fake_stream(_self, _command, on_line, **_kwargs):
        on_line("mount failed\n")
        return 7, "mount failed\n"

    monkeypatch.setattr(ColabCli, "run_interactive_streaming", fake_stream)
    emitted = []
    transport = CliTransport(
        lambda phase, status, message, data=None: emitted.append(
            (phase, status, message, data or {})
        )
    )

    assert transport.mount_drive("drive-fail") is False
    assert any(
        event[0] == "log.line"
        and event[3].get("stream") == "stderr"
        and "exit 7" in event[2]
        for event in emitted
    )


def test_drive_mount_can_be_canceled_while_waiting_for_confirmation(monkeypatch):
    auth_url = "https://accounts.google.com/o/oauth2/auth?state=cancel-test"

    def fake_build(_self, *_args):
        code = (
            f"print({auth_url!r}, flush=True); "
            "open('/dev/tty').readline()"
        )
        return [sys.executable, "-c", code]

    monkeypatch.setattr(ColabCli, "build", fake_build)
    emitted = []
    transport = CliTransport(
        lambda phase, status, message, data=None: emitted.append(
            (phase, status, message, data or {})
        )
    )
    result = []
    worker = threading.Thread(
        target=lambda: result.append(transport.mount_drive("drive-cancel"))
    )
    worker.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not any(
        event[3].get("authorization_url") == auth_url for event in emitted
    ):
        time.sleep(0.01)

    assert cancel_drive_mount("drive-cancel") is True
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert result == [False]
    assert cancel_drive_mount("drive-cancel") is False


def test_drive_mount_detects_cli_error_with_zero_exit(monkeypatch):
    def fake_stream(_self, _command, on_line, **_kwargs):
        output = "ValueError: mount failed\n"
        on_line(output)
        return 0, output

    monkeypatch.setattr(ColabCli, "run_interactive_streaming", fake_stream)
    emitted = []
    transport = CliTransport(
        lambda phase, status, message, data=None: emitted.append(
            (phase, status, message, data or {})
        )
    )

    assert transport.mount_drive("drive-fail-zero") is False
    assert any("CLI 已报告 mount failed" in event[2] for event in emitted)
