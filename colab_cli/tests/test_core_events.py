"""事件协议回归：dry-run 全程事件流的完整性、顺序与契约符合性。"""

import json
import re
from pathlib import Path

import jsonschema
import pytest

import colab_core.task as core_task
from colab_core import TaskConfig, events as ev, run_task

HERE = Path(__file__).resolve().parent.parent
SCHEMA = json.loads(
    (HERE / "contracts" / "task_event.schema.json").read_text(encoding="utf-8")
)
TASK_ID = "20260822-000000-abcdef12"


@pytest.fixture
def mini_workflow(tmp_path):
    """最小可用工作流配置 + api.json + 输入素材。"""
    (tmp_path / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    api_json = tmp_path / "api.json"
    api_json.write_text(
        json.dumps({
            "1": {
                "class_type": "LoadImage",
                "inputs": {"image": "frame.png"},
            },
            "2": {
                "class_type": "SaveVideo",
                "inputs": {"images": ["1", 0]},
            },
        }),
        encoding="utf-8",
    )
    config_path = tmp_path / "workflow.json"
    config_path.write_text(
        json.dumps({
            "name": "测试工作流",
            "api_json": "api.json",
            "supported_accelerators": ["gpu"],
            "input_fields": [
                {"class_type": "LoadImage", "field": "image"}
            ],
            "models": [
                {
                    "repo": "Comfy-Org/Test",
                    "filename": "vae/test_vae.safetensors",
                    "comfy_dir": "vae",
                    "cache_path": "vae/test_vae.safetensors",
                    "size_bytes": 1024,
                }
            ],
        }),
        encoding="utf-8",
    )
    return config_path


@pytest.fixture
def dry_run_events(tmp_path, mini_workflow, monkeypatch):
    """跑一次完整 dry-run 并收集全部事件。"""
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    events = []
    cfg = TaskConfig(
        workflow_config=mini_workflow,
        output_dir=tmp_path / "out" / TASK_ID,
        task_id=TASK_ID,
        dry_run=True,
    )
    result = run_task(cfg, events.append)
    return events, result


def test_task_event_contract_v1_frozen():
    """P0.1：schema / Python PHASES / TS 类型 三端 phase+status 锁定为 v=1。"""
    assert SCHEMA["properties"]["v"]["const"] == ev.PROTOCOL_VERSION == 1
    assert SCHEMA["properties"]["phase"]["enum"] == list(ev.PHASES)
    assert SCHEMA["properties"]["status"]["enum"] == list(ev.STATUSES)
    ts_types = (HERE / "web" / "types" / "events.d.ts").read_text(encoding="utf-8")
    for phase in ev.PHASES:
        assert f'"{phase}"' in ts_types
    for status in ev.STATUSES:
        assert f'"{status}"' in ts_types


def test_events_validate_against_schema(dry_run_events):
    events, _ = dry_run_events
    assert len(events) > 30
    for event in events:
        jsonschema.validate(event, SCHEMA)


def test_seq_strictly_increasing_from_zero(dry_run_events):
    events, _ = dry_run_events
    assert [event["seq"] for event in events] == list(range(len(events)))


def test_envelope_fields(dry_run_events):
    events, _ = dry_run_events
    pattern = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
    for event in events:
        assert event["v"] == 1
        assert pattern.match(event["task_id"])
        assert isinstance(event["message"], str)
        assert isinstance(event["data"], dict)


def test_phase_sequence_and_terminal(dry_run_events):
    events, _ = dry_run_events
    phases = [event["phase"] for event in events]
    # 生命周期主干顺序
    backbone = [
        "task.start",
        "input.resolve",
        "session.create",
        "drive.mount",
        "input.upload",
        "env.setup",
        "node.prepare",
        "model.prepare",
        "comfy.start",
        "workflow.exec",
        "output.pack",
        "output.transfer",
        "session.stop",
        "task.done",
    ]
    positions = []
    for phase in backbone:
        assert phase in phases, f"缺少阶段 {phase}"
        positions.append(phases.index(phase))
    assert positions == sorted(positions), "阶段顺序违反生命周期"
    assert events[-1]["phase"] == "task.done"
    assert events[-1]["status"] == "ok"


def test_metrics_and_progress_payloads(dry_run_events):
    events, _ = dry_run_events
    metrics = [e for e in events if e["phase"] == "metrics.sample"]
    assert metrics, "dry-run 应产出 metrics.sample"
    assert metrics[0]["data"]["gpu_util"] > 0
    progress = [
        e for e in events
        if e["phase"] == "workflow.exec" and e["status"] == "progress"
    ]
    assert progress
    assert all("percent" in e["data"] for e in progress)


def test_output_artifacts(dry_run_events):
    events, result = dry_run_events
    assert result.status == "succeeded"
    assert result.output_zip.is_file()
    transfer_ok = [
        e for e in events
        if e["phase"] == "output.transfer" and e["status"] == "ok"
    ]
    assert transfer_ok
    assert transfer_ok[-1]["data"]["verified"] is True
    assert transfer_ok[-1]["data"]["sha256"] == result.output_sha256


def test_failure_event_on_bad_config(tmp_path, monkeypatch):
    """可预期失败：error 事件 + task.failed 终态 + TaskError 抛出。"""
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    from colab_core import TaskError

    events = []
    cfg = TaskConfig(
        workflow_config=tmp_path / "missing.json",
        output_dir=tmp_path / "out" / TASK_ID,
        task_id=TASK_ID,
        dry_run=True,
    )
    with pytest.raises(TaskError):
        run_task(cfg, events.append)
    assert events[-1]["phase"] == "task.failed"
    assert events[-1]["data"]["error_type"] == "TaskError"
    assert "缺少工作流配置" in events[-1]["data"]["message"]
    errors = [e for e in events if e["status"] == "error"]
    assert errors[0]["phase"] == "task.start"
    assert errors[0]["message"]  # 可预期错误带中文文案
