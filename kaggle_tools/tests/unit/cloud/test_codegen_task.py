"""固定代码生成任务: 代码提取、测试执行与隔离。不调用模型。"""

import pytest

GOOD_IMPLEMENTATION = '''\
def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并闭区间。"""
    for start, end in intervals:
        if start > end:
            raise ValueError("bad interval")
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
'''


@pytest.fixture
def task(load_task):
    return load_task("codegen_task.py")


def test_extract_code_prefers_the_longest_fenced_block(task):
    response = "说明\n```python\nx = 1\n```\n然后\n```python\ndef f():\n    return 2\n```\n"
    assert task.extract_code(response) == "def f():\n    return 2\n"


def test_extract_code_without_fence_uses_whole_text(task):
    assert task.extract_code("  y = 3  \n") == "y = 3\n"


def test_correct_implementation_passes_hidden_tests(task, tmp_path):
    (tmp_path / f"test_{task.MODULE_NAME}.py").write_text(task.TESTS, encoding="utf-8")
    (tmp_path / f"{task.MODULE_NAME}.py").write_text(GOOD_IMPLEMENTATION, encoding="utf-8")
    result = task.run_tests(tmp_path)
    assert result.returncode == 0, result.stdout


def test_broken_implementation_fails(task, tmp_path):
    (tmp_path / f"test_{task.MODULE_NAME}.py").write_text(task.TESTS, encoding="utf-8")
    (tmp_path / f"{task.MODULE_NAME}.py").write_text(
        "def merge_intervals(intervals):\n    return intervals\n", encoding="utf-8")
    assert task.run_tests(tmp_path).returncode != 0


def test_hidden_suite_size(task):
    assert task.TESTS.count("def test_") == 7


def test_verification_timeout_is_reported_not_raised(task, tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(task, "VERIFY_TIMEOUT_SECONDS", 1)
    result = task.run_command([sys.executable, "-c", "import time; time.sleep(5)"], cwd=tmp_path)
    assert result.returncode == 124
    assert "超过" in result.stderr
