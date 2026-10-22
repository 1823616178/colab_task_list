"""运行目录(时间戳 + latest)、kcloud cycle、kcloud apply。"""

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from kaggle_tools.cloud import cli
from kaggle_tools.cloud.apply import ApplyError, apply_diff, summarize_diff
from kaggle_tools.cloud.runs import latest_run, mark_latest, new_run_dir
from tests.conftest import EXAMPLES

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


# ==== 运行目录 ===============================================================


def test_run_dirs_are_timestamped_and_unique(tmp_path):
    now = datetime(2026, 10, 22, 16, 30, 5)
    first = new_run_dir(tmp_path, "cloud", "anthropic/claude-sonnet-5@default", now=now)
    second = new_run_dir(tmp_path, "cloud", "anthropic/claude-sonnet-5@default", now=now)
    assert first.name == "20261022-163005-anthropic-claude-sonnet-5-default"
    assert second.name == first.name + "-2"
    assert first.parent == tmp_path / "cloud"


def test_latest_pointers(tmp_path):
    local = new_run_dir(tmp_path, "local")
    mark_latest(tmp_path, "local", local)
    cloud = new_run_dir(tmp_path, "cloud", "m")
    mark_latest(tmp_path, "cloud", cloud)
    assert latest_run(tmp_path, "local") == local.resolve()
    assert latest_run(tmp_path, "cloud") == cloud.resolve()
    assert latest_run(tmp_path) == cloud.resolve()


def test_latest_falls_back_to_text_file(tmp_path, monkeypatch):
    def no_symlink(self, *args, **kwargs):
        raise OSError("symlinks not permitted")

    monkeypatch.setattr(Path, "symlink_to", no_symlink)
    run = new_run_dir(tmp_path, "local")
    mark_latest(tmp_path, "local", run)
    assert (tmp_path / "local" / "latest.txt").is_file()
    assert latest_run(tmp_path, "local") == run.resolve()


@pytest.fixture
def runs(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    monkeypatch.setattr(cli, "RUNS_DIRECTORY", root)
    monkeypatch.setenv("KCLOUD_RUNS_DIR", str(root))  # main() 会重新加载配置
    return root


def test_local_output_resume_uses_latest(runs):
    args = cli.build_parser().parse_args(["local", "--resume"])
    assert cli.local_output(args) is None  # 还没有上一次
    previous = new_run_dir(runs, "local")
    mark_latest(runs, "local", previous)
    assert cli.local_output(args) == previous.resolve()
    fresh = cli.local_output(cli.build_parser().parse_args(["local"]))
    assert fresh != previous.resolve() and fresh.parent == (runs / "local").resolve()


def test_download_goes_to_new_dir_and_updates_latest(runs, monkeypatch):
    calls = []

    def fake_kaggle(arguments):
        calls.append(arguments)
        output = Path(arguments[arguments.index("-o") + 1])
        (output / "changes.diff").write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
        return 0

    monkeypatch.setattr(cli, "kaggle_command", fake_kaggle)
    for _ in range(2):
        assert cli.main(["download", "-m", "openai/gpt-oss-120b"]) == 0
    dirs = sorted(p for p in (runs / "cloud").iterdir() if p.is_dir() and not p.is_symlink())
    assert len(dirs) == 2  # 不再覆盖上一次
    assert latest_run(runs, "cloud") == dirs[-1].resolve()


# ==== cycle ==================================================================


def test_cycle_runs_all_steps_in_order(runs, monkeypatch):
    seen = []

    def fake_kaggle(arguments):
        seen.append(arguments[2] if arguments[0] == "b" else arguments[0])
        if arguments[2] == "download":
            output = Path(arguments[arguments.index("-o") + 1])
            (output / "changes.diff").write_text("(无改动)\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli, "kaggle_command", fake_kaggle)
    monkeypatch.setattr(cli, "build_tasks", lambda output: [])
    assert cli.main(["cycle", "-y", "-d", "me/repo"]) == 0
    assert seen == ["push", "run", "status", "download"]


def test_cycle_stops_when_run_fails(runs, monkeypatch):
    seen = []

    def fake_kaggle(arguments):
        seen.append(arguments[2])
        return 1 if arguments[2] == "run" else 0

    monkeypatch.setattr(cli, "kaggle_command", fake_kaggle)
    assert cli.main(["cycle", "--skip-push"]) == 1
    assert seen == ["run"]


# ==== apply ==================================================================


def make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLES / "example_repo", repo)
    for command in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *command], cwd=repo, check=True)
    return repo


def make_diff(tmp_path, repo) -> Path:
    original = (repo / "textstats" / "wordcount.py").read_text()
    changed = original.replace("def ", "def  ", 1)
    import difflib

    text = "".join(difflib.unified_diff(
        original.splitlines(True), changed.splitlines(True),
        "a/textstats/wordcount.py", "b/textstats/wordcount.py"))
    text += "--- /dev/null\n+++ b/NEW.md\n@@ -0,0 +1 @@\n+hello\n"
    path = tmp_path / "changes.diff"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def git_out(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


@needs_git
def test_apply_creates_branch_and_commits(tmp_path):
    repo = make_repo(tmp_path)
    branch, summary = apply_diff(repo, make_diff(tmp_path, repo), branch="kcloud/test")
    assert branch == "kcloud/test"
    assert git_out(repo, "branch", "--show-current") == "kcloud/test"
    assert (repo / "NEW.md").read_text() == "hello\n"
    assert git_out(repo, "status", "--porcelain") == ""
    assert "NEW.md (新增)" in summary.files
    # 原分支不受影响
    assert "NEW.md" not in git_out(repo, "ls-tree", "-r", "--name-only", "main")


@needs_git
def test_apply_check_failure_leaves_repo_untouched(tmp_path):
    repo = make_repo(tmp_path)
    diff = make_diff(tmp_path, repo)
    (repo / "textstats" / "wordcount.py").write_text("totally different\n")
    subprocess.run(["git", "commit", "-qam", "drift"], cwd=repo, check=True)
    with pytest.raises(ApplyError, match="无法干净应用"):
        apply_diff(repo, diff)
    assert git_out(repo, "branch", "--show-current") == "main"
    assert not (repo / "NEW.md").exists()


@needs_git
def test_apply_refuses_dirty_tree_and_empty_diff(tmp_path):
    repo = make_repo(tmp_path)
    diff = make_diff(tmp_path, repo)
    (repo / "scratch.txt").write_text("wip")
    with pytest.raises(ApplyError, match="未提交"):
        apply_diff(repo, diff)
    empty = tmp_path / "empty.diff"
    empty.write_text("(无改动)\n", encoding="utf-8")
    with pytest.raises(ApplyError, match="没有改动"):
        apply_diff(repo, empty, allow_dirty=True)


@needs_git
def test_apply_command_uses_latest_run(tmp_path, runs, capsys):
    repo = make_repo(tmp_path)
    run = new_run_dir(runs, "cloud", "m")
    shutil.copy(make_diff(tmp_path, repo), run / "changes.diff")
    mark_latest(runs, "cloud", run)
    assert cli.main(["apply", "--repo", str(repo), "--no-commit", "-b", "kcloud/x"]) == 0
    assert "kcloud/x" in capsys.readouterr().out
    assert git_out(repo, "status", "--porcelain")  # --no-commit 留下未提交改动


def test_summarize_diff_counts():
    summary = summarize_diff("--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n-a\n+b\n+c\n"
                             "--- a/y.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n")
    assert summary.files == ("x.py", "y.py (删除)")
    assert (summary.added, summary.removed) == (2, 2)
