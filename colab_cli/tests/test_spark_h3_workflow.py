"""Exercise the checked-in LBH workflow through Runner's input snapshot path."""

import json
import shutil
from pathlib import Path

import pytest

import colab_core.task as core_task
import colab_runner.workflows as runner_workflows
from colab_core import TaskConfig, TaskError, run_task
from colab_core.inputs import apply_optional_last_frame, collect_input_references

HERE = Path(__file__).resolve().parent.parent
SLUG = "spark_h3_lbh_lightx2v"
CANVAS_NAME = "spark_h3_lbh_official_lightx2v_4step_5p2s_i2va.json"


@pytest.fixture
def lbh_project(tmp_path):
    """Copy the real workflow and first image, leaving the tail image absent."""
    config_source = HERE / "workflows" / f"{SLUG}.json"
    config = json.loads(config_source.read_text(encoding="utf-8"))
    api_source = (config_source.parent / config["api_json"]).resolve()
    for directory in ("workflows", "flows", "image"):
        (tmp_path / directory).mkdir()
    config_path = tmp_path / "workflows" / config_source.name
    shutil.copyfile(config_source, config_path)
    api_path = tmp_path / "flows" / api_source.name
    shutil.copyfile(api_source, api_path)
    api = json.loads(api_path.read_text(encoding="utf-8"))
    (tmp_path / "image" / api["9"]["inputs"]["image"]).write_bytes(b"first frame")
    return config_path, api_path, config, api


def test_runner_skips_canvas_exports(tmp_path, monkeypatch):
    """The original canvas must not appear as a second, unusable workflow."""
    shutil.copyfile(HERE / "workflows" / CANVAS_NAME, tmp_path / CANVAS_NAME)
    shutil.copyfile(HERE / "workflows" / f"{SLUG}.json", tmp_path / f"{SLUG}.json")
    monkeypatch.setattr(runner_workflows, "WORKFLOWS_DIR", tmp_path)

    workflows = runner_workflows.list_workflows()

    assert [workflow["slug"] for workflow in workflows] == [SLUG]
    assert len(workflows[0]["models"]) == 6


def test_lbh_workflow_snapshots_shared_overrides_and_both_frames(tmp_path, monkeypatch):
    """Both sampling passes use the same prompt, duration, seed and uploaded frames."""
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    config_source = HERE / "workflows" / f"{SLUG}.json"
    config = json.loads(config_source.read_text(encoding="utf-8"))
    api_source = (config_source.parent / config["api_json"]).resolve()
    source_text = api_source.read_text(encoding="utf-8")
    for directory in ("workflows", "flows", "image"):
        (tmp_path / directory).mkdir()
    config_path = tmp_path / "workflows" / config_source.name
    shutil.copyfile(config_source, config_path)
    shutil.copyfile(api_source, tmp_path / "flows" / api_source.name)
    api = json.loads(source_text)
    for node_id in ("9", "10"):
        (tmp_path / "image" / api[node_id]["inputs"]["image"]).write_bytes(
            b"\x89PNG\r\n\x1a\n test frame"
        )
    output_dir = tmp_path / "out"

    run_task(
        TaskConfig(
            workflow_config=config_path,
            workflow_overrides={"prompt": "测试双阶段提示词", "duration": "10", "seed": "1234"},
            use_last_frame=True,
            output_dir=output_dir,
            task_id="20261022-000000-abcdef12",
            dry_run=True,
        ),
        lambda event: None,
    )

    snapshot = json.loads((output_dir / "api.json").read_text(encoding="utf-8"))
    assert snapshot["14"]["inputs"]["value"] == "测试双阶段提示词"
    assert snapshot["20"]["inputs"]["value"] == 10
    assert snapshot["37"]["inputs"]["noise_seed"] == 1234
    for node_id in ("16", "19"):
        assert snapshot[node_id]["inputs"]["prompt"] == ["14", 0]
        assert snapshot[node_id]["inputs"]["length"] == ["23", 1]
    assert snapshot["25"]["inputs"]["noise"] == snapshot["26"]["inputs"]["noise"]
    # The low-res denoised video is lifted; the low-res audio bypasses the upscaler.
    assert snapshot["38"]["inputs"]["av_latent"] == ["26", 1]
    assert snapshot["18"]["inputs"]["audio_latent"] == ["38", 1]
    assert snapshot["28"]["inputs"]["latent"] == ["38", 0]
    assert len(list((output_dir / "inputs").iterdir())) == 2
    assert (output_dir / "output.zip").is_file()
    assert api_source.read_text(encoding="utf-8") == source_text


@pytest.mark.parametrize("use_last_frame", [None, False])
@pytest.mark.parametrize("has_saved_tail", [False, True])
def test_first_frame_only_ignores_missing_or_saved_tail(
    tmp_path, monkeypatch, lbh_project, use_last_frame, has_saved_tail
):
    """Default/off tasks need only the first image, even when an old tail exists."""
    monkeypatch.setattr(core_task, "_DRY_STEP_SECONDS", 0)
    config_path, api_path, _, api = lbh_project
    source_text = api_path.read_text(encoding="utf-8")
    tail_image = tmp_path / "image" / api["10"]["inputs"]["image"]
    if has_saved_tail:
        tail_image.write_bytes(b"old tail frame")
    output_dir = tmp_path / "out"

    run_task(
        TaskConfig(
            workflow_config=config_path,
            use_last_frame=use_last_frame,
            output_dir=output_dir,
            dry_run=True,
        ),
        lambda event: None,
    )

    snapshot = json.loads((output_dir / "api.json").read_text(encoding="utf-8"))
    assert "10" not in snapshot
    for node_id in ("16", "19"):
        assert "last_frame" not in snapshot[node_id]["inputs"]
        assert snapshot[node_id]["inputs"]["first_frame"] == ["9", 0]
    assert [item.name for item in (output_dir / "inputs").iterdir()] == [api["9"]["inputs"]["image"]]
    assert api_path.read_text(encoding="utf-8") == source_text
    assert tail_image.exists() is has_saved_tail


def test_enabled_tail_still_requires_its_image(tmp_path, lbh_project):
    """Explicitly enabling the tail must not silently fall back to first-only."""
    config_path, _, _, api = lbh_project
    with pytest.raises(TaskError, match=api["10"]["inputs"]["image"]):
        run_task(
            TaskConfig(workflow_config=config_path, use_last_frame=True, dry_run=True),
            lambda event: None,
        )


def test_real_task_prepares_first_only_snapshot_before_creating_vm(tmp_path, monkeypatch, lbh_project):
    """Exercise the real preflight path without creating any remote resources."""
    config_path, api_path, _, api = lbh_project
    source_text = api_path.read_text(encoding="utf-8")

    class StopBeforeCreate:
        mode = "test"

        def create_session(self, session, kind, spec):
            raise TaskError("test: stop before creating VM")

    monkeypatch.setattr(core_task, "get_transport", lambda *args, **kwargs: StopBeforeCreate())
    output_dir = tmp_path / "real-out"
    with pytest.raises(TaskError, match="stop before creating VM"):
        run_task(
            TaskConfig(workflow_config=config_path, output_dir=output_dir),
            lambda event: None,
        )

    snapshot = json.loads((output_dir / "api.json").read_text(encoding="utf-8"))
    assert "10" not in snapshot
    assert all("last_frame" not in snapshot[node_id]["inputs"] for node_id in ("16", "19"))
    assert (output_dir / "inputs" / api["9"]["inputs"]["image"]).is_file()
    assert len(list((output_dir / "inputs").iterdir())) == 1
    assert api_path.read_text(encoding="utf-8") == source_text


def test_first_frame_only_prunes_tail_transforms_but_preserves_shared_inputs(lbh_project):
    """Tail-only transform chains are removed, while reused first images survive."""
    _, _, config, api = lbh_project
    api["100"] = {"class_type": "ImageScale", "inputs": {"image": ["10", 0]}}
    for node_id in ("16", "19"):
        api[node_id]["inputs"]["last_frame"] = ["100", 0]
    assert apply_optional_last_frame(api, config, False)
    assert "100" not in api and "10" not in api

    api["16"]["inputs"]["last_frame"] = ["9", 0]
    assert apply_optional_last_frame(api, config, False)
    assert "9" in api
    assert len(collect_input_references(api, config["input_fields"])) == 1


def test_existing_workflows_keep_their_tail_by_default(lbh_project):
    """Workflows without the opt-in declaration retain their original inputs."""
    _, _, config, api = lbh_project
    config.pop("optional_last_frame")
    assert apply_optional_last_frame(api, config, None) is False
    assert len(collect_input_references(api, config["input_fields"])) == 2
    with pytest.raises(TaskError, match="不支持尾帧开关"):
        apply_optional_last_frame(api, config, False)


@pytest.mark.parametrize("value", ["false", "true", 0, 1, {}, []])
def test_last_frame_flag_rejects_non_boolean_request_values(lbh_project, value):
    """String 'false' must never be treated as a truthy enabled toggle."""
    config_path, _, _, _ = lbh_project
    with pytest.raises(TypeError, match="use_last_frame"):
        TaskConfig.from_dict({"workflow_config": str(config_path), "use_last_frame": value})
