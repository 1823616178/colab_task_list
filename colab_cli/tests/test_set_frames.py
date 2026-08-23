"""Regression tests for the first/last frame image setup tool."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import set_frames


def build_api_json(tmp_path):
    """Create a minimal API workflow with traced frame nodes."""
    workflow = {
        "114": {
            "inputs": {"image": "old_first.png"},
            "class_type": "LoadImage",
            "_meta": {"title": "加载图像"},
        },
        "122": {
            "inputs": {"image": "old_last.png"},
            "class_type": "LoadImage",
            "_meta": {"title": "加载图像"},
        },
        "105:104": {
            "inputs": {
                "prompt": "demo",
                "first_frame": ["114", 0],
                "last_frame": ["122", 0],
            },
            "class_type": "MiniMaxH3ImageToVideo",
            "_meta": {"title": "MiniMax H3 Image to Video"},
        },
    }
    api_json = tmp_path / "api.json"
    api_json.write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return api_json


def test_main_updates_both_frames(tmp_path, capsys):
    """First and last images are copied and both nodes are patched."""
    api_json = build_api_json(tmp_path)
    image_dir = tmp_path / "image"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    first = source_dir / "首帧.png"
    last = source_dir / "尾帧.png"
    first.write_bytes(b"first-bytes")
    last.write_bytes(b"last-bytes")

    set_frames.main([
        "--first", str(first),
        "--last", str(last),
        "--api-json", str(api_json),
        "--image-dir", str(image_dir),
    ])

    assert (image_dir / "首帧.png").read_bytes() == b"first-bytes"
    assert (image_dir / "尾帧.png").read_bytes() == b"last-bytes"
    workflow = json.loads(api_json.read_text(encoding="utf-8"))
    assert workflow["114"]["inputs"]["image"] == "首帧.png"
    assert workflow["122"]["inputs"]["image"] == "尾帧.png"
    output = capsys.readouterr().out
    assert "首帧: 节点 114" in output
    assert "尾帧: 节点 122" in output


def test_main_updates_only_first_frame(tmp_path):
    """Passing only --first leaves the last frame node untouched."""
    api_json = build_api_json(tmp_path)
    image_dir = tmp_path / "image"
    first = tmp_path / "new_first.jpg"
    first.write_bytes(b"first-bytes")

    set_frames.main([
        "--first", str(first),
        "--api-json", str(api_json),
        "--image-dir", str(image_dir),
    ])

    workflow = json.loads(api_json.read_text(encoding="utf-8"))
    assert workflow["114"]["inputs"]["image"] == "new_first.jpg"
    assert workflow["122"]["inputs"]["image"] == "old_last.png"


def test_copy_reuses_identical_file(tmp_path):
    """Same name with identical content reuses the existing file."""
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "frame.png").write_bytes(b"same-bytes")
    source = tmp_path / "frame.png"
    source.write_bytes(b"same-bytes")

    assert set_frames.copy_to_image_dir(source, image_dir) == "frame.png"
    assert not (image_dir / "frame_1.png").exists()


def test_copy_renames_conflicting_file(tmp_path):
    """Same name with different content gets a numeric suffix."""
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "frame.png").write_bytes(b"old-bytes")
    source = tmp_path / "frame.png"
    source.write_bytes(b"new-bytes")

    assert set_frames.copy_to_image_dir(source, image_dir) == "frame_1.png"
    assert (image_dir / "frame_1.png").read_bytes() == b"new-bytes"
    assert (image_dir / "frame.png").read_bytes() == b"old-bytes"


def test_missing_image_exits(tmp_path):
    """A missing source image aborts with a clear error."""
    with pytest.raises(SystemExit, match="图片不存在"):
        set_frames.copy_to_image_dir(tmp_path / "nope.png", tmp_path)


def test_unsupported_extension_exits(tmp_path):
    """Non-image files are rejected before copying."""
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    with pytest.raises(SystemExit, match="不支持的图片格式"):
        set_frames.copy_to_image_dir(source, tmp_path)


def test_missing_generator_node_exits(tmp_path):
    """An API workflow without the generator node aborts."""
    api_json = tmp_path / "api.json"
    api_json.write_text(json.dumps({
        "1": {"inputs": {"image": "a.png"}, "class_type": "LoadImage"},
    }), encoding="utf-8")
    with pytest.raises(SystemExit, match="MiniMaxH3ImageToVideo"):
        set_frames.update_api_json(api_json, {"first": "a.png"})


def test_main_requires_at_least_one_frame(capsys):
    """Calling without --first/--last is a usage error."""
    with pytest.raises(SystemExit):
        set_frames.main([])


def build_first_only_api_json(tmp_path):
    """Create an API workflow where only first_frame is wired."""
    workflow = {
        "114": {
            "inputs": {"image": "old_first.png"},
            "class_type": "LoadImage",
            "_meta": {"title": "加载图像"},
        },
        "105:104": {
            "inputs": {
                "prompt": "demo",
                "first_frame": ["114", 0],
            },
            "class_type": "MiniMaxH3ImageToVideo",
            "_meta": {"title": "MiniMax H3 Image to Video"},
        },
    }
    api_json = tmp_path / "api.json"
    api_json.write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return api_json


def test_first_only_workflow_accepts_first(tmp_path):
    """A workflow without last_frame wiring still accepts --first."""
    api_json = build_first_only_api_json(tmp_path)
    image_dir = tmp_path / "image"
    first = tmp_path / "first.png"
    first.write_bytes(b"first-bytes")

    set_frames.main([
        "--first", str(first),
        "--api-json", str(api_json),
        "--image-dir", str(image_dir),
    ])

    workflow = json.loads(api_json.read_text(encoding="utf-8"))
    assert workflow["114"]["inputs"]["image"] == "first.png"


def test_repo_api_json_has_last_frame_wiring():
    """P5：仓库根 api.json 已恢复 MiniMaxH3 last_frame → LoadImage 122。"""
    api_json = Path(__file__).resolve().parent.parent.parent / "api.json"
    workflow = json.loads(api_json.read_text(encoding="utf-8"))
    generator = next(
        node for node in workflow.values()
        if node.get("class_type") == "MiniMaxH3ImageToVideo"
    )
    reference = generator["inputs"]["last_frame"]
    assert isinstance(reference, list) and reference[0] == "122"
    assert workflow["122"]["class_type"] == "LoadImage"
    assert workflow["114"]["class_type"] == "LoadImage"


def test_first_only_workflow_rejects_last(tmp_path):
    """--last fails clearly when last_frame is not wired."""
    api_json = build_first_only_api_json(tmp_path)
    last = tmp_path / "last.png"
    last.write_bytes(b"last-bytes")

    with pytest.raises(SystemExit, match="last_frame"):
        set_frames.main([
            "--last", str(last),
            "--api-json", str(api_json),
            "--image-dir", str(tmp_path / "image"),
        ])
