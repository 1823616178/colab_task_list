"""补丁提交应保留无关暂存内容, 并支持 Git 引号转义的文件名。"""

import os
from pathlib import Path

import pytest

from kaggle_tools.cloud.apply import apply_diff, diff_file_paths, summarize_diff
from tests.unit.cloud.test_runs_and_apply import git_out, make_diff, make_repo, needs_git


@needs_git
def test_apply_preserves_unrelated_staged_and_unstaged_changes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    unrelated = repo / "pytest.ini"
    original = unrelated.read_text(encoding="utf-8")
    unrelated.write_text(original + "# staged\n", encoding="utf-8")
    git_out(repo, "add", "pytest.ini")
    staged = git_out(repo, "diff", "--cached", "--binary")
    unrelated.write_text(original + "# staged\n# unstaged\n", encoding="utf-8")
    unstaged = git_out(repo, "diff", "--binary")

    apply_diff(repo, make_diff(tmp_path, repo), branch="review/staged", allow_dirty=True)

    assert set(git_out(repo, "diff", "HEAD~", "HEAD", "--name-only").splitlines()) == {
        "textstats/wordcount.py", "NEW.md"
    }
    assert git_out(repo, "diff", "--cached", "--binary") == staged
    assert git_out(repo, "diff", "--binary") == unstaged


@needs_git
@pytest.mark.parametrize("name", ["中文.txt", 'quote".txt', "tab\tname.txt", "back\\slash.txt"])
@pytest.mark.parametrize("deleted", [False, True], ids=["modified", "deleted"])
def test_apply_commits_git_quoted_paths(tmp_path: Path, name: str, deleted: bool) -> None:
    if os.name == "nt" and any(character in name for character in ('"', "\t", "\\")):
        pytest.skip("Windows 不支持这些文件名字符")
    repo = make_repo(tmp_path)
    target = repo / name
    target.write_text("old\n", encoding="utf-8")
    git_out(repo, "add", "--", name)
    git_out(repo, "commit", "-qm", "add quoted path")
    if deleted:
        target.unlink()
    else:
        target.write_text("new\n", encoding="utf-8")
    text = git_out(repo, "-c", "core.quotePath=true", "diff") + "\n"
    diff = tmp_path / "quoted.diff"
    diff.write_text(text, encoding="utf-8", newline="\n")
    git_out(repo, "restore", "--", name)

    assert diff_file_paths(text) == (name,)
    suffix = " (删除)" if deleted else ""
    assert summarize_diff(text).files == (name + suffix,)
    apply_diff(repo, diff, branch="review/quoted")

    assert git_out(repo, "status", "--porcelain") == ""
    if deleted:
        assert not target.exists()
    else:
        assert target.read_text(encoding="utf-8") == "new\n"
