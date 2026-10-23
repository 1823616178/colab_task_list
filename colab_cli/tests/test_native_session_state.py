"""Native execution must use the same isolated session store as CLI commands."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from colab_transport import NativeTransport, WorkflowNotSubmittedError
from colab_transport.cli import resolve_config_path
import colab_transport.native as native


@pytest.fixture
def native_sessions(tmp_path, monkeypatch):
    """Use actual StateStore serialization while mocking only remote execution."""
    from colab_cli.common import state
    from colab_cli.state import SessionState, StateStore
    import colab_cli.runtime as runtime_module

    monkeypatch.setenv("COLAB_CLI_STATE_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("COLAB_CONFIG", raising=False)
    global_store = SimpleNamespace(get=lambda _: pytest.fail("Native execution used the global store"))
    monkeypatch.setattr(state, "_store", global_store)
    started = []
    submitted = []

    class FakeRuntime:
        def __init__(self, url, token, **kwargs):
            self.name = token.removeprefix("test-token-")
            self.kwargs = kwargs
            started.append(self.name)

        def execute_code(self, code, timeout):
            self.kwargs["on_kernel_started"](f"kernel-{self.name}")
            self.kwargs["on_session_started"](f"session-{self.name}")
            return []

        def stop(self):
            pass

    def execute_streaming(runtime, code, receiver, timeout):
        current = StateStore(str(resolve_config_path(runtime.name))).get(runtime.name)
        assert current.running is not None
        submitted.append(runtime.name)
        return []

    monkeypatch.setattr(runtime_module, "ColabRuntime", FakeRuntime)
    monkeypatch.setattr(native, "execute_runtime_streaming", execute_streaming)

    def add_session(name):
        store = StateStore(str(resolve_config_path(name)))
        store.add(SessionState(
            name=name, token=f"test-token-{name}", url=f"https://{name}.invalid",
            endpoint=f"endpoint-{name}",
        ))
        return store

    script = tmp_path / "workflow.py"
    script.write_text("print('test workflow')", encoding="utf-8")
    return SimpleNamespace(
        state=state, global_store=global_store, add=add_session,
        started=started, submitted=submitted, script=script,
        runtime_class=FakeRuntime,
    )


@pytest.mark.parametrize("layout", ["per_session", "single_file"])
def test_native_exec_reads_and_updates_the_cli_config(tmp_path, monkeypatch, native_sessions, layout):
    sessions = native_sessions
    if layout == "single_file":
        monkeypatch.delenv("COLAB_CLI_STATE_DIR")
        monkeypatch.setenv("COLAB_CONFIG", str(tmp_path / "shared.json"))
    store = sessions.add("alpha")
    transport = NativeTransport()

    assert transport.exec_workflow("alpha", sessions.script, 60, tmp_path / "out.zip") is False

    saved = store.get("alpha")
    assert saved.kernel_id == "kernel-alpha"
    assert saved.session_id == "session-alpha"
    assert saved.running is None
    assert saved.last_execution[0] == str(sessions.script)
    assert sessions.submitted == ["alpha"]
    assert sessions.state._store is sessions.global_store


def test_concurrent_native_sessions_do_not_share_global_state(tmp_path, monkeypatch, native_sessions):
    sessions = native_sessions
    stores = {name: sessions.add(name) for name in ("alpha", "beta")}
    barrier = Barrier(2)
    original = sessions.runtime_class.execute_code

    def overlap_execution(self, code, timeout):
        barrier.wait(timeout=5)
        return original(self, code, timeout)

    monkeypatch.setattr(sessions.runtime_class, "execute_code", overlap_execution)
    transport = NativeTransport()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(
            transport.exec_workflow, name, sessions.script, 60, tmp_path / f"{name}.zip",
        ) for name in stores]
        assert [future.result(timeout=10) for future in futures] == [False, False]
    for name, store in stores.items():
        saved = store.get(name)
        assert saved.kernel_id == f"kernel-{name}"
        assert saved.session_id == f"session-{name}"
        assert saved.running is None
    assert sessions.state._store is sessions.global_store


def test_missing_mapping_is_known_to_precede_workflow_submission(tmp_path, native_sessions):
    with pytest.raises(WorkflowNotSubmittedError, match="本地没有会话映射"):
        NativeTransport().exec_workflow("missing", native_sessions.script, 60, tmp_path / "out.zip")
    assert native_sessions.started == []
    assert native_sessions.submitted == []


def test_kernel_preflight_failure_never_submits_workflow(tmp_path, monkeypatch, native_sessions):
    native_sessions.add("alpha")
    monkeypatch.setattr(native, "COLAB_EXEC_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(native.time, "sleep", lambda _: None)

    def failed_preflight(self, code, timeout):
        raise ConnectionError("kernel unavailable")

    monkeypatch.setattr(native_sessions.runtime_class, "execute_code", failed_preflight)
    with pytest.raises(WorkflowNotSubmittedError, match="连接 Colab 内核失败"):
        NativeTransport().exec_workflow("alpha", native_sessions.script, 60, tmp_path / "out.zip")
    assert native_sessions.submitted == []
