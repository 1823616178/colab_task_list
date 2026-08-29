"""Runner 服务测试：FastAPI TestClient + dry_run 任务（全程不碰真实 Colab）。

覆盖 doc/05 P1 checklist：token 鉴权、health、任务全链路（202→SSE→终态→
events.jsonl 落盘过 schema）、from_seq 回放、SESSION_BUSY、queued/running
取消、metrics/log 不落盘但进 run.log、redownload 门限与流程、崩溃恢复。
"""
import json
import os
import re
import time
import zipfile
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

import colab_core.task as core_task
import colab_runner.app as runner_app
import colab_runner.manager as runner_manager
import colab_runner.sessions as runner_sessions
import colab_runner.workflows as runner_workflows
from colab_core import TaskError
from colab_core.events import EventEmitter
from colab_runner.app import create_app

HERE = Path(__file__).resolve().parent.parent
SCHEMA = json.loads(
    (HERE / "contracts" / "task_event.schema.json").read_text(encoding="utf-8")
)
TOKEN = "test-internal-token"
AUTH = {"X-Internal-Token": TOKEN}
TERMINAL = ("succeeded", "failed", "canceled")


@pytest.fixture
def mini_workflow(tmp_path):
    """最小可用工作流配置 + api.json + 输入素材（与 test_core_events 同款）。"""
    (tmp_path / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    api_json = tmp_path / "api.json"
    api_json.write_text(
        json.dumps({
            "1": {"class_type": "LoadImage", "inputs": {"image": "frame.png"}},
            "2": {"class_type": "SaveVideo", "inputs": {"images": ["1", 0]}},
            "3": {"class_type": "MiniMaxH3ImageToVideo",
                  "inputs": {"prompt": "默认 prompt"}},
        }),
        encoding="utf-8",
    )
    config_path = tmp_path / "workflow.json"
    config_path.write_text(
        json.dumps({
            "name": "测试工作流",
            "api_json": "api.json",
            "supported_accelerators": ["gpu"],
            "input_fields": [{"class_type": "LoadImage", "field": "image"}],
            "editable_fields": [{
                "key": "prompt",
                "label": "视频 Prompt",
                "class_type": "MiniMaxH3ImageToVideo",
                "field": "prompt",
                "control": "textarea",
            }],
            "models": [{
                "repo": "Comfy-Org/Test",
                "filename": "vae/test_vae.safetensors",
                "comfy_dir": "vae",
                "cache_path": "vae/test_vae.safetensors",
                "size_bytes": 1024,
            }],
        }),
        encoding="utf-8",
    )
    return config_path


def make_payload(config_path, session="s-test", **overrides):
    payload = {
        "workflow_config": str(config_path),
        "session": session,
        "accelerator": {"kind": "gpu", "spec": "T4"},
        "mount_drive": True,
        "reuse": False,
        "keep": False,
        "timeout": 60.0,
        "dry_run": True,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离输出目录 + 零延迟 dry-run 的 TestClient（默认带 token 由调用方加）。"""
    monkeypatch.setenv("COLAB_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    app = create_app(token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def submit(client, config_path, session="s-test", **overrides):
    response = client.post(
        "/tasks", json=make_payload(config_path, session, **overrides),
        headers=AUTH)
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def wait_status(client, task_id, targets=TERMINAL, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.get(f"/tasks/{task_id}", headers=AUTH).json()
        if snapshot["status"] in targets:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内到达 {targets}")


def events_on_disk(client, task_id):
    path = client.app.state.manager.output_root / task_id / "events.jsonl"
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_sse(lines):
    """把 SSE 行流解析成事件列表（忽略 keepalive 注释行）。"""
    events, data = [], []
    for line in lines:
        if line.startswith("data:"):
            data.append(line[len("data:"):].lstrip())
        elif not line:
            if data:
                events.append(json.loads("\n".join(data)))
                data = []
    return events


# ── 鉴权与健康 ───────────────────────────────────────────────


def test_missing_token_401(client):
    for method, path in (("get", "/health"), ("post", "/tasks"),
                         ("get", "/sessions"), ("get", "/workflows")):
        response = getattr(client, method)(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_wrong_token_401(client):
    response = client.get("/health", headers={"X-Internal-Token": "nope"})
    assert response.status_code == 401


def test_health(client):
    body = client.get("/health", headers=AUTH).json()
    assert body["ok"] is True
    assert body["version"]
    assert body["colab_cli_version"] == "0.6.0"  # requirements.txt 钉死
    probe = body["transport"]["probe"]
    assert set(probe) == {"contents_client", "kernel_client"}
    assert all(isinstance(value, bool) for value in probe.values())
    # 降级语义与 get_transport 一致：probe 全过才是 native
    assert body["transport"]["mode"] == ("native" if all(probe.values()) else "cli")
    assert body["running_tasks"] == 0
    assert body["recovered_inflight"] == []


def test_drive_confirmation_endpoint(client, monkeypatch, tmp_path):
    task_id = "20260823-120000-abcdef12"
    record = runner_manager.TaskRecord(
        task_id=task_id,
        session="drive-session",
        out_dir=tmp_path / task_id,
        status="running",
        current_phase="drive.mount",
    )
    client.app.state.manager._records[task_id] = record
    confirmed = []
    monkeypatch.setattr(
        runner_manager,
        "confirm_drive_mount",
        lambda session: confirmed.append(session) or True,
    )

    response = client.post(f"/tasks/{task_id}/drive-confirm", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "task_id": task_id,
        "status": "confirmed",
        "session": "drive-session",
    }
    assert confirmed == ["drive-session"]


# ── 任务主链路 ───────────────────────────────────────────────


def test_dry_run_task_full_flow(client, mini_workflow):
    task_id = submit(client, mini_workflow)
    assert re.match(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$", task_id)

    snapshot = wait_status(client, task_id)
    assert snapshot["status"] == "succeeded"
    assert snapshot["session"] == "s-test"
    assert snapshot["recoverable"] is False
    assert snapshot["output"]["path"].endswith("output.zip")

    # events.jsonl：逐条过契约 schema；seq 从 0 严格递增（允许跳号——
    # progress/metrics/log 不落盘但仍占 seq）
    persisted = events_on_disk(client, task_id)
    assert len(persisted) > 10
    for event in persisted:
        jsonschema.validate(event, SCHEMA)
    seqs = [event["seq"] for event in persisted]
    assert seqs[0] == 0
    assert seqs == sorted(set(seqs))
    assert persisted[0]["phase"] == "task.queued"  # Runner 受理时自产
    assert persisted[-1]["phase"] == "task.done"
    assert Path(persisted[-1]["data"]["output_zip"]).is_file()

    # 提交快照落盘（doc/02 §2.3.1）
    task_dir = client.app.state.manager.output_root / task_id
    assert (task_dir / "api.json").is_file()
    assert (task_dir / "workflow.json").is_file()
    assert (task_dir / "inputs" / "frame.png").is_file()

    # GET /output：本地绝对路径 JSON，不传文件体
    output = client.get(f"/tasks/{task_id}/output", headers=AUTH).json()
    assert Path(output["path"]).is_absolute()
    assert output["path"] == persisted[-1]["data"]["output_zip"]
    assert output["sha256"] == persisted[-1]["data"]["sha256"]
    with zipfile.ZipFile(output["path"]) as archive:
        assert "dry_run/result.txt" in archive.namelist()


def test_task_workflow_override_is_written_to_snapshot(client, mini_workflow):
    task_id = submit(
        client,
        mini_workflow,
        workflow_overrides={"prompt": "任务级 prompt"},
    )
    assert wait_status(client, task_id)["status"] == "succeeded"

    task_dir = client.app.state.manager.output_root / task_id
    snapshot = json.loads((task_dir / "api.json").read_text(encoding="utf-8"))
    source_path = mini_workflow.parent / "api.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    assert snapshot["3"]["inputs"]["prompt"] == "任务级 prompt"
    assert source["3"]["inputs"]["prompt"] == "默认 prompt"


def test_metrics_and_log_not_persisted_but_in_run_log(client, mini_workflow):
    task_id = submit(client, mini_workflow)
    wait_status(client, task_id)
    persisted = events_on_disk(client, task_id)
    assert {event["phase"] for event in persisted}.isdisjoint(
        {"metrics.sample", "log.line"})
    assert all(event["status"] != "progress" for event in persisted)

    run_log = client.app.state.manager.output_root / task_id / "run.log"
    text = run_log.read_text(encoding="utf-8")
    assert "工作流执行完成" in text  # dry-run 的 emitter.log 逐行落 run.log


def test_sse_replay_from_seq_matches_file(client, mini_workflow):
    """已结束任务：回放 events.jsonl 后立即关闭；from_seq 与文件一致。"""
    task_id = submit(client, mini_workflow)
    wait_status(client, task_id)
    persisted = events_on_disk(client, task_id)
    from_seq = persisted[len(persisted) // 2]["seq"]

    with client.stream("GET", f"/tasks/{task_id}/events?from_seq={from_seq}",
                       headers=AUTH) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.iter_lines())
    assert events == [event for event in persisted if event["seq"] > from_seq]
    assert events[-1]["phase"] == "task.done"


def test_sse_live_stream_until_terminal(client, mini_workflow, monkeypatch):
    """在线任务：流式收到 task.queued → … → task.done 后流关闭。

    （starlette 1.6 的 TestClient 会等 ASGI 响应全程结束才返回，
    所以订阅必然发生在任务进行中 —— 走的正是在线路径。）
    """
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0.03)
    task_id = submit(client, mini_workflow)
    with client.stream("GET", f"/tasks/{task_id}/events",
                       headers=AUTH) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        raw_lines = list(response.iter_lines())
    events = parse_sse(raw_lines)
    assert events[0]["phase"] == "task.queued"
    assert events[-1]["phase"] == "task.done"
    seqs = [event["seq"] for event in events]
    assert seqs == sorted(set(seqs))  # 严格递增（在线流含 SSE-only 事件）
    # 在线推送包含不落盘的 log.line（replay 路径没有它）
    assert any(event["phase"] == "log.line" for event in events)
    wait_status(client, task_id)
    assert client.app.state.manager.running_count() == 0


def test_session_busy_409(client, mini_workflow, monkeypatch):
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0.05)
    first = submit(client, mini_workflow, session="s-busy")
    conflict = client.post(
        "/tasks", json=make_payload(mini_workflow, session="s-busy"),
        headers=AUTH)
    assert conflict.status_code == 409
    body = conflict.json()["error"]
    assert body["code"] == "SESSION_BUSY"
    assert body["detail"]["session"] == "s-busy"
    assert body["detail"]["running_task_id"] == first

    wait_status(client, first)
    # 锁已释放：同 session 可再投
    second = submit(client, mini_workflow, session="s-busy")
    wait_status(client, second)


def test_unknown_task_404s(client):
    for method, path in (
        ("get", "/tasks/nope"),
        ("get", "/tasks/nope/events"),
        ("get", "/tasks/nope/output"),
        ("post", "/tasks/nope/cancel"),
        ("post", "/tasks/nope/redownload"),
    ):
        response = getattr(client, method)(path, headers=AUTH)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


# ── 取消 ─────────────────────────────────────────────────────


def test_queued_cancel_and_keepalive(tmp_path, monkeypatch, mini_workflow):
    """queued（未下发执行）取消 → 直接 task.canceled，不碰 VM；
    排队期间 SSE 流按 15 秒节奏发 keepalive（测试中调快）。

    starlette 1.6 的 TestClient 会把流式响应完整读完才返回，无法与流
    中途交互 —— 这里直接调用路由函数拿到 StreamingResponse，迭代其
    body_iterator（同步生成器，与任务线程并行），等价于真实 SSE 消费。
    """
    monkeypatch.setenv("COLAB_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0.2)  # A 慢，留出窗口
    monkeypatch.setattr(runner_app, "SSE_KEEPALIVE_SECONDS", 0.05)
    delivered = []
    monkeypatch.setattr(
        runner_manager, "_deliver_cancel",
        lambda session, mode: delivered.append((session, mode)) or (True, ""))

    app = create_app(token=TOKEN, max_workers=1)  # 单 worker 制造排队窗口
    with TestClient(app) as client:
        first = submit(client, mini_workflow, session="s-a")
        wait_status(client, first, targets=("running",))
        second = submit(client, mini_workflow, session="s-b")

        generator = runner_app.events_generator(
            app.state.manager, second, -1)  # 直接驱动生成器（见函数 docstring）
        assert generator is not None
        raw_text = ""
        saw_keepalive = False
        for chunk in generator:
            raw_text += chunk
            if ": keepalive" in chunk:
                saw_keepalive = True  # 排队中的任务仍在按时发 keepalive
                # B 仍在排队（worker 被 A 占用）：流内发起 cancel
                outcome = app.state.manager.cancel(second, "interrupt")
                assert outcome == "canceled"  # queued：同步收尾

        assert saw_keepalive
        streamed = parse_sse(raw_text.splitlines())
        assert streamed[0]["phase"] == "task.queued"
        assert streamed[-1]["phase"] == "task.canceled"  # 流随终态关闭

        snapshot = wait_status(client, second)
        assert snapshot["status"] == "canceled"
        assert snapshot["session_kept"] is False
        assert snapshot["recoverable"] is False
        assert delivered == []  # 未下发任何 Colab 指令（不碰 VM）

        persisted = events_on_disk(client, second)
        assert [event["phase"] for event in persisted] == [
            "task.queued", "task.canceled"]
        jsonschema.validate(persisted[-1], SCHEMA)
        assert persisted[-1]["data"]["mode"] == "interrupt"  # 默认模式
        wait_status(client, first)


def test_cancel_running_interrupt(client, mini_workflow, monkeypatch):
    """interrupt：下发 restart-kernel + 标志位让任务线程尽快退出，
    core 的伪终态（task.failed）被吞掉，唯一终态是 task.canceled。"""
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0.05)
    delivered = []
    monkeypatch.setattr(
        runner_manager, "_deliver_cancel",
        lambda session, mode: delivered.append((session, mode)) or (True, ""))

    task_id = submit(client, mini_workflow, session="s-cancel")
    wait_status(client, task_id, targets=("running",))
    response = client.post(f"/tasks/{task_id}/cancel",
                           json={"mode": "interrupt"}, headers=AUTH)
    assert response.status_code == 202
    assert response.json()["status"] == "canceling"

    snapshot = wait_status(client, task_id)
    assert snapshot["status"] == "canceled"
    assert snapshot["cancel_mode"] == "interrupt"
    # interrupt：VM 保留、可立即重投、远端可能已有部分产物 → recoverable
    assert snapshot["session_kept"] is True
    assert snapshot["recoverable"] is True
    assert delivered == [("s-cancel", "interrupt")]

    persisted = events_on_disk(client, task_id)
    phases = [event["phase"] for event in persisted]
    assert "task.failed" not in phases
    assert phases[-1] == "task.canceled"
    assert persisted[-1]["data"] == {
        "mode": "interrupt", "session": "s-cancel",
        "session_kept": True, "recoverable": True,
    }


def test_cancel_terminal_conflict_and_bad_mode(client, mini_workflow):
    task_id = submit(client, mini_workflow)
    wait_status(client, task_id)
    # mode 校验优先于状态校验：非法 mode 一律 400
    bad = client.post(f"/tasks/{task_id}/cancel", json={"mode": "nuke"},
                      headers=AUTH)
    assert bad.status_code == 400
    # 终态任务不可取消
    conflict = client.post(f"/tasks/{task_id}/cancel",
                           json={"mode": "destroy"}, headers=AUTH)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "TASK_FINISHED"


# ── redownload ───────────────────────────────────────────────


def test_failed_task_and_redownload_gate(client, mini_workflow, tmp_path):
    # 失败（session 未保留）→ recoverable=False → redownload 409
    missing = tmp_path / "no-such-config.json"
    failed_id = submit(client, missing, session="s-fail")
    snapshot = wait_status(client, failed_id)
    assert snapshot["status"] == "failed"
    assert snapshot["error"]["error_type"] == "TaskError"
    assert snapshot["session_kept"] is False
    assert snapshot["recoverable"] is False
    persisted = events_on_disk(client, failed_id)
    assert persisted[-1]["phase"] == "task.failed"
    jsonschema.validate(persisted[-1], SCHEMA)
    denied = client.post(f"/tasks/{failed_id}/redownload", headers=AUTH)
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "REDOWNLOAD_NOT_ALLOWED"

    # 成功任务同样不允许 redownload
    ok_id = submit(client, mini_workflow, session="s-ok")
    wait_status(client, ok_id)
    assert client.post(f"/tasks/{ok_id}/redownload",
                       headers=AUTH).status_code == 409


def _fake_failed_run_task(cfg, emit):
    """模拟「工作流完成但取回失败、VM 保留」的失败任务（recoverable）。"""
    emitter = EventEmitter(cfg.task_id, emit)
    emitter.emit("task.start", "start", "工作流: 测试", {"session": cfg.session})
    emitter.emit("task.start", "ok", f"任务编号: {cfg.task_id}",
                 {"task_id": cfg.task_id})
    emitter.emit("output.transfer", "error", "Contents API 下载失败（已尝试 5 次）")
    emitter.emit("task.failed", "error", "", {
        "error_type": "OutputRetrievalError",
        "message": "Contents API 下载失败（已尝试 5 次）",
        "session_kept": True,
        "recovery_hint": "POST /tasks/{id}/redownload",
    })
    raise TaskError("Contents API 下载失败（已尝试 5 次）")


class _FakeTransport:
    mode = "cli"

    def download_output(self, session, output_zip, sink):
        sink("output.transfer", "start", ">>> 开始取回输出",
             {"archive_size": 3, "archive_sha256": "ab" * 32, "parts": 1})
        with zipfile.ZipFile(output_zip, "w") as archive:
            archive.writestr("recovered.txt", "ok")
        sink("output.transfer", "progress", ">>> 下载分块 1/1",
             {"part": 1, "parts": 1, "part_name": "part-00000.bin",
              "transport": "cli"})


def test_redownload_flow(client, mini_workflow, monkeypatch):
    """recoverable → redownload → succeeded：输出落原任务目录、更新 output、
    事件流在 task.failed 之后追加 output.transfer 与第二个 task.done。"""
    monkeypatch.setattr(runner_manager, "run_task", _fake_failed_run_task)
    monkeypatch.setattr(
        runner_manager, "get_transport",
        lambda mode=None, sink=None: _FakeTransport())

    task_id = submit(client, mini_workflow, session="s-redl")
    snapshot = wait_status(client, task_id)
    assert snapshot["status"] == "failed"
    assert snapshot["recoverable"] is True  # 失败但 session_kept

    response = client.post(f"/tasks/{task_id}/redownload", headers=AUTH)
    assert response.status_code == 202
    assert response.json()["status"] == "redownloading"

    snapshot = wait_status(client, task_id, targets=("succeeded", "failed"))
    assert snapshot["status"] == "succeeded"
    output_zip = Path(snapshot["output"]["path"])
    assert output_zip.parent.name == task_id  # 输出到原任务目录
    with zipfile.ZipFile(output_zip) as archive:
        assert "recovered.txt" in archive.namelist()

    persisted = events_on_disk(client, task_id)
    for event in persisted:
        jsonschema.validate(event, SCHEMA)
    terminals = [event for event in persisted
                 if event["phase"] in ("task.done", "task.failed",
                                       "task.canceled")]
    assert [event["phase"] for event in terminals] == ["task.failed", "task.done"]
    assert terminals[-1]["data"]["redownload"] is True
    seqs = [event["seq"] for event in persisted]
    assert seqs == sorted(set(seqs))  # redownload 事件 seq 继续递增

    output = client.get(f"/tasks/{task_id}/output", headers=AUTH)
    assert output.status_code == 200
    assert output.json()["path"] == str(output_zip)


def test_redownload_after_cancel(client, mini_workflow, monkeypatch):
    """interrupt 取消（recoverable）后再 redownload：cancel_requested 必须被
    复位，否则 sink 会吞掉 redownload 的全部事件（回归：事件流停滞、
    任务卡 redownloading）。"""
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0.05)
    monkeypatch.setattr(
        runner_manager, "_deliver_cancel",
        lambda session, mode: (True, ""))
    monkeypatch.setattr(
        runner_manager, "get_transport",
        lambda mode=None, sink=None: _FakeTransport())

    task_id = submit(client, mini_workflow, session="s-cancel-redl")
    wait_status(client, task_id, targets=("running",))
    response = client.post(f"/tasks/{task_id}/cancel",
                           json={"mode": "interrupt"}, headers=AUTH)
    assert response.status_code == 202
    snapshot = wait_status(client, task_id)
    assert snapshot["status"] == "canceled"
    assert snapshot["recoverable"] is True

    response = client.post(f"/tasks/{task_id}/redownload", headers=AUTH)
    assert response.status_code == 202

    snapshot = wait_status(client, task_id, targets=("succeeded", "failed"))
    assert snapshot["status"] == "succeeded"

    persisted = events_on_disk(client, task_id)
    for event in persisted:
        jsonschema.validate(event, SCHEMA)
    terminals = [event["phase"] for event in persisted
                 if event["phase"] in ("task.done", "task.failed",
                                       "task.canceled")]
    assert terminals == ["task.canceled", "task.done"]
    seqs = [event["seq"] for event in persisted]
    assert seqs == sorted(set(seqs))


def test_redownload_after_registry_miss(client, mini_workflow, monkeypatch):
    """Runner 丢掉内存档案（重启后终态任务不在 registry）仍可 redownload：
    从 events.jsonl 水合 session/last_seq，事件 seq 继续递增。"""
    monkeypatch.setattr(runner_manager, "run_task", _fake_failed_run_task)
    monkeypatch.setattr(
        runner_manager, "get_transport",
        lambda mode=None, sink=None: _FakeTransport())

    task_id = submit(client, mini_workflow, session="s-redl-disk")
    snapshot = wait_status(client, task_id)
    assert snapshot["status"] == "failed"
    assert snapshot["recoverable"] is True
    before = events_on_disk(client, task_id)
    last_seq = before[-1]["seq"]

    manager = client.app.state.manager
    dropped = manager._records.pop(task_id)
    if dropped.event_log is not None:
        dropped.event_log.close()

    response = client.post(f"/tasks/{task_id}/redownload", headers=AUTH)
    assert response.status_code == 202, response.text

    snapshot = wait_status(client, task_id, targets=("succeeded", "failed"))
    assert snapshot["status"] == "succeeded"

    persisted = events_on_disk(client, task_id)
    for event in persisted:
        jsonschema.validate(event, SCHEMA)
    seqs = [event["seq"] for event in persisted]
    assert seqs == sorted(set(seqs))
    assert seqs[-1] > last_seq
    terminals = [event["phase"] for event in persisted
                 if event["phase"] in ("task.done", "task.failed",
                                       "task.canceled")]
    assert terminals == ["task.failed", "task.done"]


# ── 崩溃恢复 ─────────────────────────────────────────────────


def test_runner_state_recovery(tmp_path, monkeypatch, mini_workflow):
    """在飞任务写进 runner_state.json；「重启」（同目录起第二个应用）后
    标记 interrupted、健康接口暴露、session 锁保持，DELETE /sessions 解除。"""
    monkeypatch.setenv("COLAB_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0.1)
    monkeypatch.setattr(  # 崩溃模拟不涉及真实 VM，DELETE 只需解锁
        runner_app, "stop_named_session", lambda name: None)

    app_a = create_app(token=TOKEN)
    with TestClient(app_a) as client_a:
        task_id = submit(client_a, mini_workflow, session="s-rec")
        wait_status(client_a, task_id, targets=("running",))
        # 轮询 state 文件直到在飞记录落盘（started_at 赋值与文件写存在
        # 微秒级窗口，直接读可能撞上）
        state_path = tmp_path / "out" / "runner_state.json"
        deadline = time.monotonic() + 15
        while True:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            entry = (state.get("tasks") or {}).get(task_id) or {}
            if entry.get("started_at"):
                break
            assert time.monotonic() < deadline, "在飞任务未写入 runner_state.json"
            time.sleep(0.02)
        assert entry["session"] == "s-rec"
        assert entry["pid"] == os.getpid()
        assert entry["last_seq"] >= 0

        app_b = create_app(token=TOKEN)  # 模拟 Runner 重启
        with TestClient(app_b) as client_b:
            health = client_b.get("/health", headers=AUTH).json()
            assert health["recovered_inflight"] == [task_id]

            snapshot = client_b.get(f"/tasks/{task_id}", headers=AUTH).json()
            assert snapshot["status"] == "interrupted"
            assert snapshot["recovered"] is True
            assert snapshot["session"] == "s-rec"

            # 恢复的会话锁仍占用（VM 可能还在跑），同 session 新任务 409
            conflict = client_b.post(
                "/tasks", json=make_payload(mini_workflow, session="s-rec"),
                headers=AUTH)
            assert conflict.status_code == 409

            # 对账项的事件流：回放已有事件后立即关闭
            with client_b.stream("GET", f"/tasks/{task_id}/events",
                                 headers=AUTH) as response:
                events = parse_sse(response.iter_lines())
            assert events[0]["phase"] == "task.queued"

            # 确认会话停止后解除对账锁，可重新投递
            assert client_b.delete("/sessions/s-rec",
                                   headers=AUTH).json()["ok"] is True
            resubmitted = submit(client_b, mini_workflow, session="s-rec")
            wait_status(client_b, resubmitted)
        # client_a 的任务线程在后台自然跑完（dry-run，秒级）


# ── 工作流与会话 ─────────────────────────────────────────────


def test_workflows_list(client):
    body = client.get("/workflows", headers=AUTH).json()
    by_slug = {entry["slug"]: entry for entry in body}
    expected = json.loads(
        (HERE / "workflows" / "minimax_h3.json").read_text(encoding="utf-8"))
    entry = by_slug["minimax_h3"]
    assert entry["name"] == expected["name"]
    assert entry["supported_accelerators"] == ["gpu"]
    assert len(entry["models"]) == len(expected["models"])
    assert entry["total_model_bytes"] == sum(
        model["size_bytes"] for model in expected["models"])
    assert entry["api_json"].endswith("api.json")
    assert {"inso_wan2_2_14", "inso_wan2_2_14b_an2",
            "inso_wan2_2_14b_an2_lora",
            "minimax_h3_first_frame"} <= set(by_slug)
    first_frame_only = by_slug["minimax_h3_first_frame"]
    assert first_frame_only["name"] == "MiniMax H3 图生视频（仅首帧）"
    assert first_frame_only["api_json"].endswith(
        "flows/video_minimax_h3_t2v_首帧.json"
    )
    assert first_frame_only["editable_fields"][0]["key"] == "prompt"
    duration = first_frame_only["editable_fields"][1]
    assert duration["key"] == "duration"
    assert duration["node_id"] == "105:111"
    assert [option["value"] for option in duration["options"]] == [
        "5", "10", "15"
    ]


def test_minimax_first_frame_uses_input_aspect_ratio():
    """首帧先等比例缩放，视频宽高读取缩放结果，不再走固定比例节点。"""
    workflow = json.loads(
        (HERE / "flows" / "video_minimax_h3_t2v_首帧.json").read_text(
            encoding="utf-8"
        )
    )
    scale = workflow["119"]["inputs"]
    video = workflow["105:104"]["inputs"]
    assert scale["image"] == ["114", 0]
    assert scale["upscale_method"] == "lanczos"
    assert scale["megapixels"] == 0.4
    assert scale["resolution_steps"] == 32
    assert video["width"] == ["120", 0]
    assert video["height"] == ["120", 1]
    assert video["first_frame"] == ["119", 0]
    assert "115" not in workflow


def test_frames_endpoint(client, tmp_path, monkeypatch):
    """包装 set_frames.py：首帧更新成功；api.json 未接 last_frame 时拒绝 last。"""
    api_json = tmp_path / "api.json"
    api_json.write_text(
        json.dumps({
            "1": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
            "2": {"class_type": "MiniMaxH3ImageToVideo",
                  "inputs": {"first_frame": ["1", 0], "prompt": "x"}},
        }, indent=2),
        encoding="utf-8",
    )
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    (workflows_dir / "wf.json").write_text(
        json.dumps({"name": "设帧测试", "api_json": str(api_json),
                    "supported_accelerators": ["gpu"]}),
        encoding="utf-8",
    )
    image_dir = tmp_path / "image"
    monkeypatch.setattr(runner_workflows, "WORKFLOWS_DIR", workflows_dir)
    monkeypatch.setattr(runner_workflows, "IMAGE_DIR", image_dir)
    first = tmp_path / "first.png"
    first.write_bytes(b"\x89PNG fake")
    last = tmp_path / "last.png"
    last.write_bytes(b"\x89PNG fake2")

    ok = client.post("/workflows/wf/frames", json={"first": str(first)},
                     headers=AUTH)
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    updated = json.loads(api_json.read_text(encoding="utf-8"))
    assert updated["1"]["inputs"]["image"] == "first.png"
    assert (image_dir / "first.png").is_file()

    # 该 api.json 没有 last_frame 接线 → 拒绝 last（doc/02 §2.2 警告）
    denied = client.post("/workflows/wf/frames", json={"last": str(last)},
                         headers=AUTH)
    assert denied.status_code == 400
    assert denied.json()["error"]["code"] == "FRAMES_UPDATE_FAILED"
    assert "last_frame" in denied.json()["error"]["message"]

    empty = client.post("/workflows/wf/frames", json={}, headers=AUTH)
    assert empty.status_code == 400
    missing = client.post("/workflows/nope/frames",
                          json={"first": str(first)}, headers=AUTH)
    assert missing.status_code == 404
    no_file = client.post("/workflows/wf/frames",
                          json={"first": str(tmp_path / "ghost.png")},
                          headers=AUTH)
    assert no_file.status_code == 400
    assert no_file.json()["error"]["code"] == "INPUT_NOT_FOUND"


def test_sessions_endpoints(client, mini_workflow, monkeypatch):
    monkeypatch.setattr(
        runner_sessions, "list_sessions",
        lambda: [{"name": "s-rec", "status": "IDLE", "variant": "GPU",
                  "accelerator": "gpu", "endpoint": "ep-1", "reachable": True}])
    stopped, unmapped = [], []

    def fake_stop(name):
        stopped.append(name)
        return f"+ colab stop -s {name}"

    monkeypatch.setattr(runner_app, "stop_named_session", fake_stop)
    monkeypatch.setattr(
        runner_app, "stop_unmapped_endpoint", unmapped.append)

    # 进行中任务的 session 标注 task_id
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0.05)
    task_id = submit(client, mini_workflow, session="s-rec")
    wait_status(client, task_id, targets=("running",))
    entries = client.get("/sessions", headers=AUTH).json()
    assert entries[0]["name"] == "s-rec"
    assert entries[0]["task_id"] == task_id
    wait_status(client, task_id)

    stopped_body = client.delete("/sessions/s-rec", headers=AUTH).json()
    assert stopped_body["ok"] is True
    assert stopped_body["name"] == "s-rec"
    assert "colab stop" in stopped_body["log"]
    assert stopped == ["s-rec"]
    assert client.delete("/sessions/by-endpoint/ep-1",
                         headers=AUTH).json() == {"ok": True,
                                                  "endpoint": "ep-1"}
    assert unmapped == ["ep-1"]


def test_create_session(client, monkeypatch):
    captured = {}

    def fake_create(name, kind, spec, mount_drive=False):
        captured.update(name=name, kind=kind, spec=spec, mount_drive=mount_drive)
        return {
            "ok": True,
            "name": name,
            "accelerator": {"kind": kind, "spec": spec},
            "mount_drive": mount_drive,
            "log": f"+ colab new -s {name} --gpu {spec}",
        }

    monkeypatch.setattr(runner_app, "create_named_session", fake_create)
    created = client.post(
        "/sessions",
        json={"name": "comfyui-h3", "accelerator": {"kind": "gpu", "spec": "L4"},
              "mount_drive": True},
        headers=AUTH,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["ok"] is True
    assert body["name"] == "comfyui-h3"
    assert "colab new" in body["log"]
    assert captured == {"name": "comfyui-h3", "kind": "gpu", "spec": "L4",
                        "mount_drive": True}

    missing = client.post("/sessions", json={}, headers=AUTH)
    assert missing.status_code == 400


def test_create_named_session_validates():
    from colab_core.sessions import create_named_session
    with pytest.raises(ValueError, match="会话名"):
        create_named_session("bad name", "gpu", "T4")
    with pytest.raises(ValueError, match="规格"):
        create_named_session("ok", "gpu", "Nope")
    with pytest.raises(ValueError, match="加速器"):
        create_named_session("ok", "xpu", "T4")


def test_create_session_busy(client, mini_workflow, monkeypatch):
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0.05)
    monkeypatch.setattr(
        runner_app, "create_named_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应创建")))
    task_id = submit(client, mini_workflow, session="s-busy")
    wait_status(client, task_id, targets=("running",))
    denied = client.post(
        "/sessions",
        json={"name": "s-busy", "accelerator": {"kind": "gpu", "spec": "T4"}},
        headers=AUTH,
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "SESSION_BUSY"
    wait_status(client, task_id)


def test_sessions_error_mapping(client, monkeypatch):
    def fail_stop(name):
        raise RuntimeError(f"停止会话失败: {name}")

    def fail_value(endpoint):
        raise ValueError(f"Runtime endpoint 格式无效: {endpoint!r}")

    monkeypatch.setattr(runner_app, "stop_named_session", fail_stop)
    monkeypatch.setattr(runner_app, "stop_unmapped_endpoint", fail_value)
    assert client.delete("/sessions/x", headers=AUTH).status_code == 500
    assert client.delete("/sessions/x",
                         headers=AUTH).json()["error"]["code"] == (
        "SESSION_STOP_FAILED")
    bad = client.delete("/sessions/by-endpoint/bad%20ep", headers=AUTH)
    assert bad.status_code == 400


def test_parse_session_line():
    parse = runner_sessions.parse_session_line
    busy = parse("[comfyui-h3] ep-abc | Hardware: GPU | Variant: A100 "
                 "| Status: BUSY (remote_workflow.py)")
    assert busy == {"name": "comfyui-h3", "endpoint": "ep-abc",
                    "variant": "A100", "accelerator": "gpu", "status": "BUSY"}
    idle = parse("[s1] ep-1 | Hardware: CPU | Variant: DEFAULT | Status: IDLE")
    assert idle["status"] == "IDLE"
    assert idle["accelerator"] == "cpu"
    unknown = parse("[?] ep-2 | Hardware: TPU | Variant: TPU")
    assert unknown["name"] == "?"
    assert unknown["status"] == "UNKNOWN"
    assert unknown["accelerator"] == "tpu"
    assert parse("[colab] No active sessions found on server.") is None


def test_relative_paths_anchored_to_repo(client, tmp_path, monkeypatch,
                                         mini_workflow):
    """相对路径锚定仓库根（HERE），与 Runner 的工作目录无关。"""
    nested = tmp_path / "wf"
    nested.mkdir()
    for name in ("workflow.json", "api.json", "frame.png"):
        (nested / name).write_bytes(
            (mini_workflow.parent / name).read_bytes())
    monkeypatch.setattr(runner_manager, "HERE", tmp_path)
    task_id = submit(client, "wf/workflow.json", session="s-rel")
    assert wait_status(client, task_id)["status"] == "succeeded"
