"""契约实现任务的隔离保证与工具约束。

这个评测的有效性完全建立在「模型读不到验收测试」上, 所以这条要有测试兜着。
"""

import sys
from pathlib import Path

import pytest

CLOUD_CODEGEN_DIR = Path(__file__).resolve().parents[1]
TASKS_DIR = CLOUD_CODEGEN_DIR / "tasks"


@pytest.fixture
def task(tmp_path, monkeypatch):
    """加载任务模块并把工作区与验收目录指到临时目录。"""
    monkeypatch.setenv("CODEGEN_IMPORT_ONLY", "1")
    monkeypatch.syspath_prepend(str(TASKS_DIR))
    sys.modules.pop("spec_task", None)
    import spec_task

    monkeypatch.setattr(spec_task, "OUTPUT_DIRECTORY", tmp_path)
    monkeypatch.setattr(spec_task, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(spec_task, "ACCEPTANCE_DIRECTORY", tmp_path / "_acceptance")
    spec_task.prepare_directories()
    return spec_task


def test_acceptance_tests_live_outside_the_workspace(task):
    acceptance = task.ACCEPTANCE_DIRECTORY.resolve()
    assert not acceptance.is_relative_to(task.WORKSPACE.resolve())
    assert (acceptance / "test_usage_client.py").is_file()


def test_model_cannot_read_the_acceptance_tests(task):
    for attempt in (
        "../_acceptance/test_usage_client.py",
        "../../_acceptance/test_usage_client.py",
        "/etc/passwd",
    ):
        assert task.read_file(attempt).startswith("ERROR"), attempt


def test_model_cannot_overwrite_the_acceptance_tests(task):
    original = (task.ACCEPTANCE_DIRECTORY / "test_usage_client.py").read_text()
    assert task.write_file("../_acceptance/test_usage_client.py", "# nope").startswith("ERROR")
    assert (task.ACCEPTANCE_DIRECTORY / "test_usage_client.py").read_text() == original


def test_workspace_writes_and_reads_round_trip(task):
    assert task.write_file("usage_client.py", "VALUE = 1\n").startswith("OK")
    assert task.read_file("usage_client.py") == "VALUE = 1\n"
    assert "usage_client.py" in task.list_files()


def test_verification_fails_when_nothing_is_implemented(task):
    result = task.execute_verification()
    assert result.returncode != 0


def test_spec_is_readable_through_the_tool(task, monkeypatch, tmp_path):
    spec = tmp_path / "usage-api.md"
    spec.write_text("# 契约\n路由: /api/usage\n")
    monkeypatch.setenv("SPEC_PATH", str(spec))
    assert "api/usage" in task.read_spec()


def test_missing_spec_fails_loudly(task, monkeypatch, tmp_path):
    monkeypatch.setenv("SPEC_PATH", str(tmp_path / "missing.md"))
    with pytest.raises(FileNotFoundError):
        task.locate_spec()


def test_acceptance_suite_covers_the_documented_contract(task):
    tests = task.ACCEPTANCE_TESTS
    assert tests.count("def test_") == 12
    for name in ("get_usage", "get_providers", "set_providers", "remaining",
                 "UnknownProviderError", "UsageError"):
        assert name in tests, name


def test_tools_expose_clean_schemas(task):
    from kaggle_benchmarks.tools.functions import function_to_openai_tool

    names = set()
    for tool in task.TOOLS:
        schema = function_to_openai_tool(tool)["function"]
        assert schema["description"], f"{schema['name']} 缺少 docstring"
        names.add(schema["name"])
    assert names == {"read_spec", "list_files", "read_file", "write_file", "run_verification"}
