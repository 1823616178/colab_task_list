"""大文件防护: 截断提示、按行读取、edit_file、禁止整份覆盖; diff 可被 git apply。"""

import shutil
import subprocess

import pytest

from tests.conftest import EXAMPLES


@pytest.fixture
def task(load_task):
    module = load_task("repo_task.py")
    module.prepare_workspace(EXAMPLES / "example_repo")
    return module


def big_file(task, name="big.py", lines=5000):
    content = "".join(f"LINE_{i} = {i}\n" for i in range(1, lines + 1))
    (task.WORKSPACE / name).write_text(content, encoding="utf-8")
    return content


def test_truncated_read_says_so(task):
    content = big_file(task)
    assert len(content) > task.MAX_FILE_BYTES
    result = task.read_file("big.py")
    assert "[已截断" in result
    assert f"共 {len(content)} 字符" in result


def test_small_file_has_no_notice(task):
    assert "[已截断" not in task.read_file("textstats/wordcount.py")


def test_line_range_read(task):
    big_file(task)
    result = task.read_file("big.py", start_line=4999, end_line=5000)
    assert result.startswith("[第 4999-5000 行, 共 5000 行]")
    assert "LINE_5000 = 5000" in result
    assert "LINE_1 =" not in result


def test_invalid_line_range(task):
    big_file(task, lines=10)
    assert task.read_file("big.py", start_line=8, end_line=3).startswith("ERROR")


def test_full_overwrite_of_truncated_file_is_blocked(task):
    original = big_file(task)
    result = task.write_file("big.py", "LINE_1 = 1\n")
    assert result.startswith("ERROR")
    assert (task.WORKSPACE / "big.py").read_text(encoding="utf-8") == original


def test_edit_file_changes_a_unique_snippet(task):
    big_file(task)
    assert task.edit_file("big.py", "LINE_4000 = 4000\n", "LINE_4000 = -1\n").startswith("OK")
    text = (task.WORKSPACE / "big.py").read_text(encoding="utf-8")
    assert "LINE_4000 = -1\n" in text and text.endswith("LINE_5000 = 5000\n")


def test_edit_file_rejects_ambiguous_or_missing(task):
    (task.WORKSPACE / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    assert "2 次" in task.edit_file("dup.py", "x = 1\n", "y\n")
    assert task.edit_file("dup.py", "nope", "y").startswith("ERROR")
    assert task.edit_file("missing.py", "a", "b").startswith("ERROR")


def test_edit_file_respects_protection(task):
    assert task.edit_file("tests/test_wordcount.py", "def", "xx").startswith("ERROR")


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_diff_with_add_delete_and_no_newline_applies_with_git(task, tmp_path):
    repo = tmp_path / "target"
    shutil.copytree(EXAMPLES / "example_repo", repo)
    snapshot = task.read_tree(repo)
    task.write_file("textstats/new_module.py", "VALUE = 1")             # 新增, 无末尾换行
    (task.WORKSPACE / "textstats" / "__init__.py").unlink()             # 删除
    task.edit_file("textstats/wordcount.py", "def ", "def  ")           # 修改
    diff = task.build_diff(snapshot)
    assert "--- /dev/null" in diff and "+++ /dev/null" in diff
    assert "\\ No newline at end of file" in diff

    patch = tmp_path / "changes.diff"
    patch.write_text(diff, encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=repo, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=repo, check=True)
    assert (repo / "textstats" / "new_module.py").read_text() == "VALUE = 1"
    assert not (repo / "textstats" / "__init__.py").exists()
