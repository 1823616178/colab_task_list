"""跨进程账本和包含文件头样式正文的补丁回归。"""

import difflib
import multiprocessing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kaggle_tools.cloud.apply import apply_diff, diff_file_paths, summarize_diff
from kaggle_tools.kllm.usage import Ledger, append_record, archive_old_records, load_records
from tests.unit.cloud.test_runs_and_apply import git_out, make_repo, needs_git

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def usage_record(cost, model="m"):
    return {"ts": NOW.isoformat(), "model": model, "cost_nanodollars": cost * 10**9}


def test_record_includes_external_usage_before_updating_fingerprint(tmp_path):
    path = tmp_path / "usage.jsonl"
    ledger = Ledger(path, timezone.utc, daily_limit=10)
    append_record(path, usage_record(12, "cli"))
    ledger.record(usage_record(1, "gateway"))
    for _ in range(2):
        status = ledger.status(NOW)
        assert status.today.cost_dollars == 13
        assert status.today.call_count == 2
        assert status.exceeded
        assert ledger.model_breakdown(now=NOW)["cli"]["cost_dollars"] == 12
    assert Ledger(path, timezone.utc).status(NOW).today == status.today


def _archive_paused_before_replace(path, ready, release):
    original_replace = Path.replace

    def paused_replace(source, target):
        ready.set()
        if not release.wait(15):
            raise RuntimeError("test did not release archive")
        return original_replace(source, target)

    with patch.object(Path, "replace", paused_replace):
        archive_old_records(path, "2026-01")


def _write_in_process(path, started, finished, use_ledger):
    started.set()
    if use_ledger:
        Ledger(path, timezone.utc).record(usage_record(12, "concurrent"))
    else:
        append_record(path, usage_record(12, "concurrent"))
    finished.set()


@pytest.mark.parametrize("use_ledger", [False, True])
def test_archive_preserves_concurrent_process_write(tmp_path, use_ledger):
    path = tmp_path / "usage.jsonl"
    old = {"ts": "2025-12-01T00:00:00+00:00", "model": "old"}
    append_record(path, old)
    append_record(path, usage_record(1))
    context = multiprocessing.get_context("spawn")
    ready, release, started, finished = [context.Event() for _ in range(4)]
    archiver = context.Process(target=_archive_paused_before_replace,
                               args=(path, ready, release))
    writer = context.Process(target=_write_in_process,
                             args=(path, started, finished, use_ledger))
    archiver.start()
    try:
        assert ready.wait(15)
        writer.start()
        assert started.wait(15)
        assert not finished.wait(0.5), "writer must wait until archive replaces the ledger"
        release.set()
        archiver.join(15)
        writer.join(15)
        assert archiver.exitcode == writer.exitcode == 0
    finally:
        release.set()
        for process in (archiver, writer):
            if process.pid is not None:
                process.join(2)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
    assert load_records(path) == [usage_record(1), usage_record(12, "concurrent")]
    assert load_records(path.with_name("usage-archive.jsonl")) == [old]


def test_diff_headers_in_body_do_not_become_paths():
    text = ("--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n"
            "--- removed text\n+++ added text\n"
            "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n--- gone\n"
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+++ new\n"
            "\\ No newline at end of file\n")
    assert diff_file_paths(text) == ("x.txt", "gone.txt", "new.txt")
    summary = summarize_diff(text)
    assert summary.files == ("x.txt", "gone.txt (删除)", "new.txt (新增)")
    assert (summary.added, summary.removed) == (2, 2)


@needs_git
def test_apply_commits_patch_with_header_like_body(tmp_path):
    repo = make_repo(tmp_path)
    original = (repo / "pytest.ini").read_text(encoding="utf-8")
    changed = original + "++ added text\n"
    text = "".join(difflib.unified_diff(original.splitlines(True), changed.splitlines(True),
                                        "a/pytest.ini", "b/pytest.ini"))
    diff = tmp_path / "changes.diff"
    diff.write_text(text, encoding="utf-8", newline="\n")
    apply_diff(repo, diff, branch="kcloud/header-body")
    assert (repo / "pytest.ini").read_text(encoding="utf-8") == changed
    assert git_out(repo, "status", "--porcelain") == ""
    assert git_out(repo, "diff", "HEAD~", "HEAD", "--name-only") == "pytest.ini"
