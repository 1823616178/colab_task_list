"""提交快照回归（doc/02 §2.3.1）：受理后修改 api.json 不影响已提交任务。"""

import json

import pytest

import colab_core.task as core_task
from colab_core import TaskConfig, run_task

TASK_ID = "20260822-000000-abcdef12"


@pytest.fixture
def mini_project(tmp_path):
    (tmp_path / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    api_json = tmp_path / "api.json"
    api_json.write_text(
        json.dumps({
            "1": {
                "class_type": "LoadImage",
                "inputs": {"image": "frame.png"},
            },
            "2": {
                "class_type": "MiniMaxH3ImageToVideo",
                "inputs": {"prompt": "默认 prompt"},
            },
            "3": {
                "class_type": "PrimitiveFloat",
                "inputs": {"value": 5},
            },
        }, indent=2),
        encoding="utf-8",
    )
    config_path = tmp_path / "workflow.json"
    config_path.write_text(
        json.dumps({
            "name": "快照测试",
            "api_json": "api.json",
            "supported_accelerators": ["gpu"],
            "input_fields": [{"class_type": "LoadImage", "field": "image"}],
            "editable_fields": [{
                "key": "prompt",
                "class_type": "MiniMaxH3ImageToVideo",
                "field": "prompt",
            }, {
                "key": "duration",
                "node_id": "3",
                "field": "value",
                "value_type": "number",
                "options": [
                    {"label": "5 秒", "value": "5"},
                    {"label": "10 秒", "value": "10"},
                    {"label": "15 秒", "value": "15"},
                ],
            }],
        }),
        encoding="utf-8",
    )
    return config_path, api_json


def test_submission_snapshots_inputs(tmp_path, mini_project, monkeypatch):
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    config_path, api_json = mini_project
    out_dir = tmp_path / "out" / TASK_ID

    run_task(
        TaskConfig(
            workflow_config=config_path,
            output_dir=out_dir,
            task_id=TASK_ID,
            dry_run=True,
        ),
        lambda event: None,
    )

    snapshot_api = out_dir / "api.json"
    snapshot_config = out_dir / "workflow.json"
    snapshot_input = out_dir / "inputs" / "frame.png"
    assert snapshot_api.is_file()
    assert snapshot_config.is_file()
    assert snapshot_input.is_file()

    # 受理后篡改源文件：快照必须仍是提交瞬间的内容。
    # api.json / workflow.json 是复制，源文件就地覆写也不受影响；
    # 输入素材是硬链接（doc/06 省空间），可防 os.replace 原子换名
    # （set_frames.py 的覆写方式），但不防就地截断写。
    api_json.write_text('{"9": {"class_type": "Hacked"}}', encoding="utf-8")
    replaced = tmp_path / "frame.new"
    replaced.write_bytes(b"tampered")
    replaced.replace(tmp_path / "frame.png")
    config_path.write_text("{}", encoding="utf-8")

    assert json.loads(snapshot_api.read_text(encoding="utf-8"))["1"][
        "class_type"
    ] == "LoadImage"
    assert json.loads(snapshot_config.read_text(encoding="utf-8"))[
        "name"
    ] == "快照测试"
    assert snapshot_input.read_bytes() == b"\x89PNG\r\n\x1a\n fake"


def test_workflow_override_only_changes_task_snapshot(
    tmp_path, mini_project, monkeypatch
):
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    config_path, api_json = mini_project
    out_dir = tmp_path / "out" / TASK_ID

    run_task(
        TaskConfig(
            workflow_config=config_path,
            workflow_overrides={"prompt": "本次任务的新 prompt"},
            output_dir=out_dir,
            task_id=TASK_ID,
            dry_run=True,
        ),
        lambda event: None,
    )

    source = json.loads(api_json.read_text(encoding="utf-8"))
    snapshot = json.loads((out_dir / "api.json").read_text(encoding="utf-8"))
    assert source["2"]["inputs"]["prompt"] == "默认 prompt"
    assert snapshot["2"]["inputs"]["prompt"] == "本次任务的新 prompt"


def test_unknown_workflow_override_is_rejected(
    tmp_path, mini_project, monkeypatch
):
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    config_path, _ = mini_project

    with pytest.raises(core_task.TaskError, match="不支持参数覆盖: unknown"):
        run_task(
            TaskConfig(
                workflow_config=config_path,
                workflow_overrides={"unknown": "value"},
                output_dir=tmp_path / "out" / TASK_ID,
                task_id=TASK_ID,
                dry_run=True,
            ),
            lambda event: None,
        )


def test_numeric_workflow_override_is_converted_in_task_snapshot(
    tmp_path, mini_project, monkeypatch
):
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    config_path, api_json = mini_project
    out_dir = tmp_path / "out" / TASK_ID

    run_task(
        TaskConfig(
            workflow_config=config_path,
            workflow_overrides={"duration": "10"},
            output_dir=out_dir,
            task_id=TASK_ID,
            dry_run=True,
        ),
        lambda event: None,
    )

    source = json.loads(api_json.read_text(encoding="utf-8"))
    snapshot = json.loads((out_dir / "api.json").read_text(encoding="utf-8"))
    assert source["3"]["inputs"]["value"] == 5
    assert snapshot["3"]["inputs"]["value"] == 10
    assert isinstance(snapshot["3"]["inputs"]["value"], int)


def test_workflow_override_rejects_value_outside_options(
    tmp_path, mini_project, monkeypatch
):
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    config_path, _ = mini_project

    with pytest.raises(core_task.TaskError, match="duration 不在允许的选项中"):
        run_task(
            TaskConfig(
                workflow_config=config_path,
                workflow_overrides={"duration": "12"},
                output_dir=tmp_path / "out" / TASK_ID,
                task_id=TASK_ID,
                dry_run=True,
            ),
            lambda event: None,
        )


def test_cli_emit_prints_nothing_for_empty_message(tmp_path, capsys):
    """CLI emit：空 message（仅服务端里程碑）不产生任何输出。"""
    from colab_core import EventEmitter, make_cli_emit

    emitter = EventEmitter(TASK_ID, make_cli_emit())
    emitter.emit("session.create", "start", "", {"session": "s"})
    emitter.emit("task.start", "start", "工作流: X")
    emitter.emit("task.start", "error", "出错了")

    captured = capsys.readouterr()
    assert captured.out == "工作流: X\n"
    assert captured.err == "出错了\n"
