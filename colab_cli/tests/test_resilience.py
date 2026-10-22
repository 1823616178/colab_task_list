"""colab_core.resilience：瞬时故障分类、退避重试、执行阶段断线 reattach。"""
import pytest

from colab_core.resilience import (
    backoff_delay,
    is_transient_error,
    reattach_workflow,
    retry_transient,
)
from colab_transport import TransportError


@pytest.mark.parametrize("message", [
    "Connection was lost.",
    "HTTPSConnectionPool: Max retries exceeded",
    "502 Bad Gateway",
    "Read timed out",
    "websocket connection closed unexpectedly",
    "colab exec 失败: ECONNRESET",
])
def test_transient_transport_errors(message):
    assert is_transient_error(TransportError(message))


@pytest.mark.parametrize("error", [
    TransportError("会话 's1' 已不可访问，无法自动重试"),
    TransportError("输出 ZIP sha256 校验失败"),
    TransportError("colab exec 失败 (exit 1, 尝试 3/3)"),
    ValueError("connection lost"),  # 非通道类异常不按文案判断
])
def test_permanent_errors(error):
    assert not is_transient_error(error)


def test_builtin_network_errors_are_transient():
    assert is_transient_error(ConnectionResetError("x"))
    assert is_transient_error(TimeoutError())


def test_backoff_delay_exponential_capped():
    assert [backoff_delay(n, 5) for n in (1, 2, 3)] == [5, 10, 20]
    assert backoff_delay(10, 5) == 60


def test_retry_transient_recovers_and_reports():
    calls, retries, sleeps = [], [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransportError("Connection was lost.")
        return "ok"

    result = retry_transient(
        flaky, step="上传", attempts=3, base_delay=1,
        on_retry=lambda a, t, d, e: retries.append((a, t, d)),
        sleep=sleeps.append)
    assert result == "ok"
    assert retries == [(1, 3, 1), (2, 3, 2)]
    assert sleeps == [1, 2]


def test_retry_transient_gives_up_and_skips_permanent():
    with pytest.raises(TransportError):
        retry_transient(lambda: (_ for _ in ()).throw(TransportError("timed out")),
                        step="x", attempts=2, base_delay=0, sleep=lambda _: None)
    calls = []

    def permanent():
        calls.append(1)
        raise TransportError("permission denied")

    with pytest.raises(TransportError):
        retry_transient(permanent, step="x", attempts=5, sleep=lambda _: None)
    assert len(calls) == 1


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_reattach_succeeds_when_manifest_appears():
    clock = FakeClock()
    readiness = iter([False, False, True])
    waits = []
    ok = reattach_workflow(
        session_alive=lambda: True,
        output_ready=lambda: next(readiness),
        on_wait=lambda elapsed, limit: waits.append(elapsed),
        timeout=600, poll=30, sleep=clock.sleep, clock=clock)
    assert ok is True
    assert waits == [0, 30]


def test_reattach_fails_fast_when_session_gone_or_unsupported():
    assert reattach_workflow(session_alive=lambda: False, output_ready=lambda: True,
                             timeout=600, poll=1, sleep=lambda _: None) is False
    assert reattach_workflow(session_alive=lambda: True, output_ready=lambda: None,
                             timeout=600, poll=1, sleep=lambda _: None) is False


def test_reattach_times_out():
    clock = FakeClock()
    assert reattach_workflow(session_alive=lambda: True, output_ready=lambda: False,
                             timeout=90, poll=30, sleep=clock.sleep, clock=clock) is False
    assert clock.now == 90


def test_reattach_disabled_with_zero_timeout():
    assert reattach_workflow(session_alive=lambda: True, output_ready=lambda: True,
                             timeout=0) is False


# ── run_task 集成：假通道注入瞬时故障 ──────────────────────────────

import json  # noqa: E402
import zipfile  # noqa: E402

from colab_core import TaskConfig, run_task  # noqa: E402
from colab_core import task as core_task  # noqa: E402


class FlakyTransport:
    """上传首次瞬时失败；执行阶段断线；远端产物第二次探测时就绪。"""

    mode = "cli"

    def __init__(self, exec_error=None):
        self.upload_failures = 1
        self.exec_error = exec_error
        self.ready_polls = 0
        self.downloads = 0

    def create_session(self, session, kind, spec):
        pass

    def mount_drive(self, session):
        return True

    def stop_session(self, session):
        pass

    def session_status(self, session):
        return "RUNNING"

    def check_session_readable(self, session, sink=None):
        pass

    def upload(self, session, local_path, remote_path):
        if self.upload_failures:
            self.upload_failures -= 1
            raise TransportError("Connection was lost.")

    def exec_script(self, session, script_path, timeout, sink=None):
        pass

    def exec_workflow(self, session, script_path, timeout, output_zip, sink=None):
        if self.exec_error is not None:
            raise self.exec_error
        return False

    def remote_output_ready(self, session):
        self.ready_polls += 1
        return self.ready_polls >= 2

    def download_output(self, session, output_zip, sink=None):
        self.downloads += 1
        with zipfile.ZipFile(output_zip, "w") as archive:
            archive.writestr("out.mp4", b"video")


@pytest.fixture
def workflow_files(tmp_path):
    (tmp_path / "frame.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "api.json").write_text(json.dumps({
        "1": {"class_type": "LoadImage", "inputs": {"image": "frame.png"}},
    }), encoding="utf-8")
    config = tmp_path / "workflow.json"
    config.write_text(json.dumps({
        "name": "测试", "api_json": "api.json",
        "input_fields": [{"class_type": "LoadImage", "field": "image"}],
    }), encoding="utf-8")
    return config


def _cfg(tmp_path, config):
    cfg = TaskConfig.from_dict({
        "workflow_config": str(config), "session": "s-flaky",
        "accelerator": {"kind": "gpu", "spec": "T4"},
        "mount_drive": False, "reuse": True, "keep": False, "timeout": 60.0,
        "input_dir": str(tmp_path),
    })
    cfg.output_dir = tmp_path / "out"
    return cfg


def _patch(monkeypatch, transport):
    monkeypatch.setattr(core_task, "get_transport", lambda *a, **k: transport)
    monkeypatch.setenv("COLAB_RETRY_BASE_SECONDS", "0")
    monkeypatch.setenv("COLAB_REATTACH_POLL_SECONDS", "0")
    monkeypatch.setattr("colab_core.resilience.time.sleep", lambda _: None)


def test_run_task_retries_upload_and_reattaches(tmp_path, workflow_files, monkeypatch):
    transport = FlakyTransport(exec_error=TransportError("websocket connection closed"))
    _patch(monkeypatch, transport)
    events = []
    result = run_task(_cfg(tmp_path, workflow_files), events.append)
    assert result.status == "succeeded"
    assert transport.downloads == 1 and transport.ready_polls == 2
    reasons = [(e.get("data") or {}).get("reason") for e in events]
    assert "transient_retry" in reasons
    assert "reattach_start" in reasons and "reattach_ok" in reasons
    assert events[-1]["phase"] == "task.done"


def test_run_task_permanent_exec_error_not_reattached(tmp_path, workflow_files, monkeypatch):
    transport = FlakyTransport(exec_error=TransportError("colab exec 失败 (exit 1, 尝试 3/3)"))
    _patch(monkeypatch, transport)
    events = []
    with pytest.raises(TransportError):
        run_task(_cfg(tmp_path, workflow_files), events.append)
    assert transport.ready_polls == 0
    assert events[-1]["phase"] == "task.failed"
