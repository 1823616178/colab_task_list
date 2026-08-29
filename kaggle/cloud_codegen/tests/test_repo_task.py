"""仓库改造任务的工作区、路径防护与 diff 生成。

不调用模型: 只测任务里那些纯逻辑的部分。
"""

import sys
from pathlib import Path

import pytest

CLOUD_CODEGEN_DIR = Path(__file__).resolve().parents[1]
TASKS_DIR = CLOUD_CODEGEN_DIR / "tasks"
EXAMPLE_REPO = CLOUD_CODEGEN_DIR / "examples" / "example_repo"


@pytest.fixture
def task(tmp_path, monkeypatch):
    """加载任务模块并把工作区指到临时目录。"""
    monkeypatch.setenv("CODEGEN_IMPORT_ONLY", "1")
    monkeypatch.syspath_prepend(str(TASKS_DIR))
    sys.modules.pop("repo_task", None)
    import repo_task

    monkeypatch.setattr(repo_task, "OUTPUT_DIRECTORY", tmp_path)
    monkeypatch.setattr(repo_task, "WORKSPACE", tmp_path / "workspace")
    return repo_task


def test_workspace_snapshot_only_tracks_repo_files(task):
    snapshot = task.prepare_workspace(EXAMPLE_REPO)
    assert set(snapshot) == {
        "pytest.ini",
        "tests/test_wordcount.py",
        "textstats/__init__.py",
        "textstats/wordcount.py",
    }


def test_original_repository_is_never_touched(task):
    before = (EXAMPLE_REPO / "textstats" / "wordcount.py").read_text()
    task.prepare_workspace(EXAMPLE_REPO)
    task.write_file("textstats/wordcount.py", "# clobbered\n")
    assert (EXAMPLE_REPO / "textstats" / "wordcount.py").read_text() == before


def test_protected_paths_reject_writes(task):
    task.prepare_workspace(EXAMPLE_REPO)
    assert task.write_file("tests/test_wordcount.py", "x").startswith("ERROR")
    assert task.write_file("pytest.ini", "x").startswith("ERROR")
    # 受保护的文件仍然可读, 模型要能看见测试想干什么以外的上下文。
    assert "def test_" in task.read_file("tests/test_wordcount.py")


def test_path_traversal_is_rejected(task):
    task.prepare_workspace(EXAMPLE_REPO)
    assert task.write_file("../escape.py", "x").startswith("ERROR")
    assert task.read_file("../../etc/passwd").startswith("ERROR")
    assert not (task.WORKSPACE.parent / "escape.py").exists()


def test_writes_inside_workspace_succeed(task):
    task.prepare_workspace(EXAMPLE_REPO)
    assert task.write_file("textstats/extra.py", "VALUE = 1\n").startswith("OK")
    assert (task.WORKSPACE / "textstats" / "extra.py").read_text() == "VALUE = 1\n"


def test_verification_reports_failure_before_any_fix(task):
    task.prepare_workspace(EXAMPLE_REPO)
    assert task.execute_verification().returncode != 0


def test_diff_ignores_caches_generated_by_verification(task):
    snapshot = task.prepare_workspace(EXAMPLE_REPO)
    task.execute_verification()  # 会生成 .pytest_cache
    task.write_file("textstats/marker.py", "MARKER = True\n")
    diff = task.build_diff(snapshot)
    assert ".pytest_cache" not in diff
    assert "textstats/marker.py" in diff


def test_diff_is_empty_when_nothing_changed(task):
    snapshot = task.prepare_workspace(EXAMPLE_REPO)
    assert task.build_diff(snapshot) == ""


def test_list_files_excludes_caches(task):
    task.prepare_workspace(EXAMPLE_REPO)
    task.execute_verification()
    listing = task.list_files()
    assert "__pycache__" not in listing
    assert ".pytest_cache" not in listing
    assert "textstats/wordcount.py" in listing


def test_locate_repository_honors_environment_override(task, monkeypatch, tmp_path):
    target = tmp_path / "some_repo"
    target.mkdir()
    monkeypatch.setenv("CODEGEN_REPO_PATH", str(target))
    assert task.locate_repository() == target.resolve()


def test_locate_repository_fails_loudly_when_missing(task, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_REPO_PATH", str(tmp_path / "nope"))
    with pytest.raises(FileNotFoundError):
        task.locate_repository()


def test_tools_expose_clean_schemas(task):
    from kaggle_benchmarks.tools.functions import function_to_openai_tool

    names = set()
    for tool in task.TOOLS:
        schema = function_to_openai_tool(tool)["function"]
        assert schema["description"], f"{schema['name']} 缺少 docstring"
        names.add(schema["name"])
    assert names == {"list_files", "read_file", "write_file", "run_verification"}
