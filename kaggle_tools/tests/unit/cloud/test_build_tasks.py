"""单文件任务构建: 生成物不依赖兄弟模块, 能在空目录里独立 import。"""

import os
import subprocess
import sys

import pytest

from kaggle_tools.cloud.build import SOURCE_DIRECTORY, TASK_FILES, BuildError, render_task


@pytest.mark.parametrize("name", TASK_FILES)
def test_built_task_has_no_sibling_import(built_tasks_dir, name):
    text = (built_tasks_dir / name).read_text(encoding="utf-8")
    assert "from _workspace_tools import" not in text
    assert "def resolve_inside_workspace" in text
    assert text.startswith("# ⚠ 由 kcloud build 生成")


@pytest.mark.parametrize("name", TASK_FILES)
def test_built_task_imports_standalone(built_tasks_dir, tmp_path, name):
    """拷到一个只有它自己的目录里, 用全新解释器 import, 模拟 Kaggle 云端。"""
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / name).write_bytes((built_tasks_dir / name).read_bytes())
    environment = {**os.environ, "CODEGEN_IMPORT_ONLY": "1", "PYTHONPATH": ""}
    completed = subprocess.run(
        [sys.executable, "-I", "-c", f"import runpy; runpy.run_path({name!r})"],
        cwd=isolated, env=environment, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr


def test_source_tasks_contain_exactly_one_inline_marker():
    for name in TASK_FILES:
        render_task(name)  # 不抛即合格


def test_missing_marker_is_rejected(tmp_path):
    (tmp_path / "_workspace_tools.py").write_text('"""doc"""\nX = 1\n', encoding="utf-8")
    (tmp_path / "bad_task.py").write_text("print('no marker')\n", encoding="utf-8")
    with pytest.raises(BuildError):
        render_task("bad_task.py", tmp_path)


def test_build_script_and_kcloud_build_agree(tmp_path):
    from kaggle_tools.cloud import cli

    assert cli.main(["build", "-o", str(tmp_path)]) == 0
    for name in TASK_FILES:
        assert (tmp_path / name).read_text(encoding="utf-8") == render_task(name)
    assert (SOURCE_DIRECTORY / "_workspace_tools.py").is_file()
