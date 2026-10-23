"""EventLog 单元测试：轮转、节流、ring→文件回退、慢消费者断开。"""
import json
from pathlib import Path

import jsonschema
import pytest

import colab_runner.store as store
from colab_core.events import EventEmitter
from colab_runner.store import EventLog, read_events_file

TASK_ID = "20260822-000000-abcdef12"
SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent
     / "contracts" / "task_event.schema.json").read_text(encoding="utf-8")
)


@pytest.fixture
def event_log(tmp_path):
    log = EventLog(TASK_ID, tmp_path / TASK_ID)
    yield log
    log.close()


def _emit_milestones(log: EventLog, count: int) -> list:
    """经 core 的 EventEmitter 发 count 个落盘事件，返回收集到的事件。"""
    collected = []
    emitter = EventEmitter(TASK_ID, lambda event: collected.append(log.emit(event)))
    for index in range(count):
        emitter.emit("workflow.exec", "start" if index == 0 else "ok",
                     f"里程碑 {index}")
    return collected


def test_event_log_start_seq_continues(tmp_path):
    """磁盘水合后 start_seq=已有最后 seq，新事件继续递增不回头。"""
    log = EventLog(TASK_ID, tmp_path / TASK_ID)
    log.emit(store.build_event(TASK_ID, "task.queued", "start", "已受理"))
    log.emit(store.build_event(
        TASK_ID, "task.failed", "error", "", {"session_kept": True}))
    last = log.last_seq
    log.close()
    resumed = EventLog(TASK_ID, tmp_path / TASK_ID, start_seq=last)
    event = resumed.emit(store.build_event(
        TASK_ID, "task.done", "ok", "", {"redownload": True}))
    assert event["seq"] == last + 1
    resumed.close()


def test_seq_restamped_from_runner_queue_position(event_log):
    """Runner 侧 task.queued 占 seq=0 后，core 事件统一重盖为 1,2,3..."""
    event_log.emit(store.build_event(TASK_ID, "task.queued", "start", "已受理"))
    collected = _emit_milestones(event_log, 3)
    assert [event["seq"] for event in collected] == [1, 2, 3]
    on_disk = read_events_file(event_log.events_path)
    assert [event["seq"] for event in on_disk] == [0, 1, 2, 3]


def test_run_log_rotation(tmp_path, monkeypatch):
    """64 MiB 上限（测试中调小）：滚动为 run.log.1，并补 warn 事件。"""
    monkeypatch.setattr(store, "RUN_LOG_MAX_BYTES", 200)
    log = EventLog(TASK_ID, tmp_path / TASK_ID)
    emitter = EventEmitter(TASK_ID, log.emit)
    for _ in range(10):
        emitter.log("x" * 50)
    log.close()

    backup = log.run_log_path.with_name("run.log.1")
    assert backup.is_file()
    assert log.run_log_path.is_file()
    assert log.run_log_path.stat().st_size <= 200 + 51  # 上限 + 最后一条

    persisted = read_events_file(log.events_path)
    rotations = [
        event for event in persisted
        if event["status"] == "warn"
        and event["data"].get("reason") == "run_log_rotated"
    ]
    assert rotations, "轮转必须向事件流补 warn 事件（doc/02 §2.6）"
    for event in persisted:
        jsonschema.validate(event, SCHEMA)


def test_throttle_drops_intermediate_progress(event_log, monkeypatch):
    """progress 按 phase 1 秒节流：丢弃中间帧；log.line 不节流。"""
    monkeypatch.setattr(store, "THROTTLE_SECONDS", 100.0)
    _, _, subscriber = event_log.subscribe(from_seq=-1)
    emitter = EventEmitter(TASK_ID, event_log.emit)
    for step in range(5):
        emitter.emit("workflow.exec", "progress", f"步骤 {step}",
                     {"percent": step * 20.0})
        emitter.emit("metrics.sample", "progress", "", {"gpu_util": step})
        emitter.log(f"日志行 {step}")

    pushed = []
    while not subscriber.empty():
        pushed.append(subscriber.get_nowait())
    progress_pushed = [e for e in pushed if e["status"] == "progress"
                       and e["phase"] != "log.line"]
    logs_pushed = [e for e in pushed if e["phase"] == "log.line"]
    # 每个 phase 只放行第一帧
    assert {(e["phase"], e["seq"]) for e in progress_pushed} == {
        ("workflow.exec", 0), ("metrics.sample", 1)}
    assert len(logs_pushed) == 5  # log.line 逐行全推

    monkeypatch.setattr(store, "THROTTLE_SECONDS", 0.0)
    emitter.emit("workflow.exec", "progress", "最后一步", {"percent": 100.0})
    assert subscriber.get_nowait()["message"] == "最后一步"


def test_replay_falls_back_to_file_when_ring_rolled(tmp_path, monkeypatch):
    """from_seq 落在 ring 窗口外时回文件读（doc/02 §2.5 硬性要求）。"""
    monkeypatch.setattr(store, "RING_BUFFER_SIZE", 3)  # 构造时生效
    log = store.EventLog(TASK_ID, tmp_path / TASK_ID)
    _emit_milestones(log, 10)  # 落盘 seq 0..9；ring 只剩末尾 3 条

    # ring 窗口外的 from_seq 必须回文件拿到全部
    replay_all = log.replay(0)
    assert [event["seq"] for event in replay_all] == list(range(1, 10))
    # from_seq=8 在窗口内：走 ring，结果一致
    assert [event["seq"] for event in log.replay(8)] == [9]
    log.close()


def test_progress_and_metrics_not_persisted(event_log):
    """metrics.sample / log.line / progress 一律不写 events.jsonl。"""
    emitter = EventEmitter(TASK_ID, event_log.emit)
    emitter.emit("task.start", "start", "开始")
    emitter.emit("model.prepare", "progress", "下载中", {"done_bytes": 1})
    emitter.emit("metrics.sample", "progress", "", {"gpu_util": 90})
    emitter.log("一行日志")
    emitter.emit("task.done", "ok", "", {"task_id": TASK_ID})

    persisted = read_events_file(event_log.events_path)
    assert [event["phase"] for event in persisted] == ["task.start", "task.done"]
    # 但被丢帧仍占 seq：文件里的 seq 允许跳号（0, 4）
    assert [event["seq"] for event in persisted] == [0, 4]


def test_slow_subscriber_is_disconnected(event_log, monkeypatch):
    """订阅队列积满即断开（慢消费者走 from_seq 重连补拉，不拖垮任务线程）。"""
    monkeypatch.setattr(store, "SUBSCRIBER_QUEUE_SIZE", 1)
    _, _, subscriber = event_log.subscribe(from_seq=-1)
    emitter = EventEmitter(TASK_ID, event_log.emit)
    emitter.emit("task.start", "start", "一")
    emitter.emit("task.start", "ok", "二")  # 队列已满 → 断开
    emitter.emit("task.done", "ok", "", {"task_id": TASK_ID})  # 不再投递
    assert subscriber.qsize() == 1
    assert subscriber.get_nowait() is None  # 实际通知生成器关闭，不能只移除注册
    assert event_log._subscribers == []
