"""Runner 故障边界回归；所有云端操作均替换为本地桩。"""
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import colab_core.task as core_task
import colab_runner.manager as manager_module
import colab_runner.sessions as sessions
import colab_runner.store as store
from colab_core import TaskError
from colab_runner.app import create_app, events_generator
from colab_runner.manager import (
    RedownloadNotAllowedError,
    SessionBusyError,
    TaskManager,
)
from colab_runner.store import build_event
from colab_transport import TransportError


@pytest.fixture
def manager(tmp_path, monkeypatch):
    mgr = TaskManager(output_root=tmp_path)
    mgr.scheduled = []
    monkeypatch.setattr(mgr._executor, "submit", lambda *args: mgr.scheduled.append(args))
    yield mgr
    mgr.shutdown()


def submit(mgr, session="test", **kwargs):
    return mgr.submit({"workflow_config": "missing.json", "session": session, **kwargs})


def emit(record, phase, status="ok", **data):
    return record.event_log.emit(build_event(record.task_id, phase, status, data=data))


@pytest.mark.parametrize("error", [TaskError("early"), TransportError("early"), None])
def test_worker_always_finishes(manager, monkeypatch, error):
    def run(*args):
        if error:
            raise error
    monkeypatch.setattr(manager_module, "run_task", run)
    record = submit(manager)
    manager._execute(record)
    assert record.status == "failed"
    assert record.terminal
    assert manager.task_id_for_session(record.session) is None
    assert manager.get(record.task_id).error["message"]


def test_transport_initialization_failure_has_terminal(manager, monkeypatch):
    def fail(*args, **kwargs):
        raise TransportError("initialization failed")
    monkeypatch.setattr(core_task, "get_transport", fail)
    record = submit(manager)
    manager._execute(record)
    assert record.status == "failed"
    assert record.error["message"] == "initialization failed"
    assert not record.session_kept
    assert [e["phase"] for e in store.read_events_file(record.event_log.events_path)][-1] == "task.failed"


def test_slow_stream_closes_and_reconnect_replays(manager, monkeypatch):
    monkeypatch.setattr(store, "SUBSCRIBER_QUEUE_SIZE", 1)
    record = submit(manager)
    stream = events_generator(manager, record.task_id, -1)
    assert "task.queued" in next(stream)
    emit(record, "workflow.exec", "start")
    emit(record, "workflow.exec", "ok")
    emit(record, "task.failed", "error", session_kept=False)
    with pytest.raises(StopIteration):
        next(stream)
    frames = list(events_generator(manager, record.task_id, 0))
    assert len(frames) == 3
    assert '"phase": "task.failed"' in frames[-1]


def test_subscribe_after_terminal_at_watermark_closes(tmp_path):
    log = store.EventLog("test", tmp_path)
    event = log.emit(build_event("test", "task.done", "ok"))
    replay, _, queue = log.subscribe(event["seq"])
    assert replay == []
    assert queue.get_nowait() is None
    log.close()


def test_rotation_keeps_live_sequence_monotonic(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "RUN_LOG_MAX_BYTES", 8)
    log = store.EventLog("test", tmp_path)
    _, _, queue = log.subscribe(-1)
    for _ in range(4):
        log.emit(build_event("test", "log.line", "progress", "123456"))
    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [item["seq"] for item in items] == sorted({item["seq"] for item in items})
    log.close()


def test_interrupted_survives_multiple_restarts_and_releases_all(tmp_path):
    state = tmp_path / "runner_state.json"
    state.write_text(json.dumps({"tasks": {key: {"session": "shared"} for key in ("a", "b")}}))
    for _ in range(2):
        mgr = TaskManager(output_root=tmp_path)
        mgr.recover()
        assert set(mgr.recovered_inflight) == {"a", "b"}
        assert mgr.task_id_for_session("shared") is not None
        mgr.shutdown()
    mgr = TaskManager(output_root=tmp_path)
    mgr.recover()
    mgr.release_recovered_session("shared")
    assert mgr.recovered_inflight == []
    assert mgr.task_id_for_session("shared") is None
    mgr.shutdown()
    final = TaskManager(output_root=tmp_path)
    final.recover()
    assert final.recovered_inflight == []
    assert final.get("a").status == "failed"
    final.shutdown()


def test_durable_terminal_wins_over_stale_running_state(manager):
    record = submit(manager)
    emit(record, "task.done", output_zip="output.zip", session_kept=False)
    # 模拟 terminal 已 fsync、线程还没清理 runner_state 的进程崩溃。
    recovered = TaskManager(output_root=manager.output_root)
    recovered.recover()
    assert recovered.recovered_inflight == []
    assert recovered.get(record.task_id).status == "succeeded"
    assert recovered.task_id_for_session(record.session) is None
    recovered.shutdown()


@pytest.mark.parametrize("reuse", [False, True])
def test_preserved_failure_blocks_automatic_queue(manager, monkeypatch, reuse):
    first = submit(manager)
    second = submit(manager, reuse=reuse)
    def fail(cfg, sink):
        sink(build_event(cfg.task_id, "task.failed", "error", data={"session_kept": True}))
        raise TaskError("lost channel")
    monkeypatch.setattr(manager_module, "run_task", fail)
    manager._execute(first)
    assert first.recoverable
    assert second.status == "failed"
    assert second.error["error_type"] == "SESSION_RECOVERY_REQUIRED"
    assert len(manager.scheduled) == 1
    assert manager.task_id_for_session(first.session) == first.task_id
    with pytest.raises(SessionBusyError):
        submit(manager)
    # 用户处理后明确复用可继续；不自动修改排队任务的 reuse。
    third = submit(manager, reuse=True)
    assert manager.task_id_for_session(first.session) == third.task_id


def test_preserved_owner_survives_restart(manager, monkeypatch):
    record = submit(manager)
    monkeypatch.setattr(manager_module, "run_task", lambda cfg, sink: sink(
        build_event(cfg.task_id, "task.failed", "error", data={"session_kept": True})))
    manager._execute(record)
    recovered = TaskManager(output_root=manager.output_root)
    recovered.recover()
    assert recovered.task_id_for_session(record.session) == record.task_id
    assert recovered.get(record.task_id).recoverable
    recovered.release_recovered_session(record.session)
    assert not recovered.get(record.task_id).recoverable
    assert not recovered._record_from_disk(record.task_id).recoverable
    recovered.shutdown()


def test_redownload_never_holds_manager_while_waiting_record(manager):
    record = submit(manager)
    underlying = threading.Lock()
    attempting = threading.Event()
    class ObservedLock:
        def __enter__(self):
            attempting.set()
            underlying.acquire()
        def __exit__(self, *args):
            underlying.release()
    record.lock = ObservedLock()
    result = []
    def request():
        try:
            manager.redownload(record.task_id)
        except RedownloadNotAllowedError:
            result.append("rejected")
    underlying.acquire()
    thread = threading.Thread(target=request, daemon=True)
    thread.start()
    assert attempting.wait(1)
    acquired = manager._lock.acquire(timeout=1)
    if acquired:
        manager._lock.release()
    underlying.release()
    thread.join(2)
    assert acquired, "redownload 等待 record.lock 时占用了 manager._lock"
    assert result == ["rejected"]


def test_redownload_start_is_durable_and_crash_is_interrupted(manager):
    record = submit(manager)
    emit(record, "task.failed", "error", session_kept=True)
    record.status = "failed"
    record.event_log.close()
    manager.redownload(record.task_id)
    assert record.finished_at is None
    assert record.error is None
    recovered = TaskManager(output_root=manager.output_root)
    recovered.recover()
    assert recovered.get(record.task_id).status == "interrupted"
    recovered.shutdown()


def test_finished_cache_is_bounded_but_disk_history_survives(manager, monkeypatch):
    monkeypatch.setattr(manager_module, "FINISHED_CACHE_SIZE", 2)
    monkeypatch.setattr(manager_module, "run_task", lambda cfg, sink: sink(
        build_event(cfg.task_id, "task.done", "ok", data={"duration_ms": 42})))
    ids = []
    for _ in range(5):
        record = submit(manager)
        ids.append(record.task_id)
        manager._execute(record)
    assert len(manager._records) == 2
    assert manager._active_ids == set()
    old = manager.get(ids[0])
    assert old.status == "succeeded"
    assert old.duration_ms == 42
    assert old.current_phase == "task.done"
    assert manager._ensure_live_record(ids[0]).task_id == ids[0]


def test_noncritical_writes_coalesce_and_shutdown_flushes(manager, monkeypatch):
    monkeypatch.setattr(manager_module, "STATE_DEBOUNCE_SECONDS", 60)
    record = submit(manager)
    before = manager.state_path.read_text()
    for _ in range(20):
        emit(record, "model.prepare", "ok")
    assert manager.state_path.read_text() == before
    assert manager._state_timer is not None
    manager.shutdown()
    state = json.loads(manager.state_path.read_text())
    assert state["tasks"][record.task_id]["last_seq"] == record.last_seq
    assert manager._state_timer is None


@pytest.fixture(autouse=True)
def reset_session_cache():
    sessions.invalidate_sessions()
    yield
    sessions.invalidate_sessions()


@pytest.mark.parametrize("output", [None, "", "unexpected format"])
def test_cloud_failure_is_not_empty_success(monkeypatch, output):
    monkeypatch.setattr(sessions, "_capture", lambda *a, **k: output)
    monkeypatch.setattr(sessions, "_local_sessions", lambda: {"old": {"endpoint": "ep"}})
    with pytest.raises(sessions.SessionQueryError):
        sessions.list_sessions()


def test_session_query_failure_http_503(tmp_path, monkeypatch):
    def fail():
        raise sessions.SessionQueryError("offline")
    monkeypatch.setattr(sessions, "list_sessions", fail)
    with TestClient(create_app(output_root=tmp_path, token="test")) as client:
        response = client.get("/sessions", headers={"X-Internal-Token": "test"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SESSION_QUERY_FAILED"


def test_stopped_local_sessions_do_not_trigger_probes(monkeypatch):
    calls = []
    def capture(*args, **kwargs):
        calls.append(args[1:])
        return "[colab] No active sessions found on server."
    monkeypatch.setattr(sessions, "_capture", capture)
    monkeypatch.setattr(sessions, "_local_sessions", lambda: {
        str(i): {"endpoint": str(i)} for i in range(100)})
    entries = sessions.list_sessions()
    assert len(entries) == 100
    assert not any(entry["reachable"] for entry in entries)
    assert calls == [("sessions",)]


def test_cloud_alive_status_failure_remains_reachable(monkeypatch):
    def capture(cli, command, *args, **kwargs):
        return "[?] ep | Hardware: L4 | Variant: GPU" if command == "sessions" else None
    monkeypatch.setattr(sessions, "_capture", capture)
    monkeypatch.setattr(sessions, "_local_sessions", lambda: {"live": {"endpoint": "ep"}})
    assert sessions.list_sessions() == [{"name": "live", "endpoint": "ep", "variant": "L4",
                                        "accelerator": "gpu", "status": "UNKNOWN", "reachable": True}]


def test_session_cache_singleflight_copies_and_invalidation(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def query():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return [{"name": "one"}]
    monkeypatch.setattr(sessions, "_query_sessions", query)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(sessions.list_sessions) for _ in range(8)]
        assert entered.wait(1)
        release.set()
        results = [future.result() for future in futures]
    assert len(calls) == 1
    results[0][0]["name"] = "mutated"
    assert sessions.list_sessions()[0]["name"] == "one"
    sessions.invalidate_sessions()
    sessions.list_sessions()
    assert len(calls) == 2


def test_failed_session_queries_are_not_cached(monkeypatch):
    calls = []
    def query():
        calls.append(1)
        if len(calls) == 1:
            raise sessions.SessionQueryError("offline")
        return []
    monkeypatch.setattr(sessions, "_query_sessions", query)
    with pytest.raises(sessions.SessionQueryError):
        sessions.list_sessions()
    assert sessions.list_sessions() == []
    assert len(calls) == 2


def test_cli_query_has_subprocess_timeout(monkeypatch):
    def run(command, **kwargs):
        assert kwargs["timeout"] == 0.1
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(sessions.subprocess, "run", run)
    assert sessions._capture(SimpleNamespace(build=lambda *a: list(a)), "sessions", timeout=0.1) is None


def test_partial_cloud_parse_failure_is_not_partial_snapshot(monkeypatch):
    output = "[one] ep1 | Hardware: L4 | Variant: GPU\n[two] ep2 | Hardware: A100"
    monkeypatch.setattr(sessions, "_capture", lambda *a, **k: output)
    monkeypatch.setattr(sessions, "_local_sessions", lambda: {})
    with pytest.raises(sessions.SessionQueryError):
        sessions.list_sessions()


def test_redownload_waits_until_previous_worker_closes_log(manager, monkeypatch):
    record = submit(manager)
    monkeypatch.setattr(manager_module, "run_task", lambda cfg, sink: sink(
        build_event(cfg.task_id, "task.failed", "error", data={"session_kept": True})))
    closing = threading.Event()
    allow_close = threading.Event()
    real_close = record.event_log.close
    def close():
        closing.set()
        assert allow_close.wait(2)
        real_close()
    monkeypatch.setattr(record.event_log, "close", close)
    worker = threading.Thread(target=manager._execute, args=(record,), daemon=True)
    worker.start()
    assert closing.wait(1)
    requests = []
    request = threading.Thread(target=lambda: requests.append(manager.redownload(record.task_id)), daemon=True)
    request.start()
    allow_close.set()
    worker.join(2)
    request.join(2)
    assert not worker.is_alive() and not request.is_alive()
    assert requests == [record]
    assert record.status == "redownloading"
    assert not record.event_log._stream_ended
    assert store.read_events_file(record.event_log.events_path)[-1]["data"]["redownload"]


@pytest.mark.parametrize("value", [False, 0, [], {}, "", "bogus"])
def test_runner_rejects_invalid_model_mode_before_acceptance(tmp_path, value):
    with TestClient(create_app(output_root=tmp_path, token="test")) as client:
        response = client.post("/tasks", headers={"X-Internal-Token": "test"}, json={
            "workflow_config": "missing.json", "model_mode": value,
        })
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"
        assert client.app.state.manager.running_count() == 0
        assert client.app.state.manager._records == {}
