"""仓库改造任务的工作区、路径防护与 diff 生成。

不调用模型: 只测任务里那些纯逻辑的部分。
"""

import json

import pytest

from tests.conftest import EXAMPLES

EXAMPLE_REPO = EXAMPLES / "example_repo"


@pytest.fixture
def task(load_task):
    """加载构建后的仓库改造任务, 工作区指到临时目录。"""
    return load_task("repo_task.py")


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


# ==== 运行期 job 配置 =======================================================


def test_job_defaults_when_nothing_is_configured(task, tmp_path):
    assert task.load_job(tmp_path / "no-input") == task.DEFAULT_SPEC
    assert task.REPO_NAME == task.DEFAULT_REPO_NAME
    assert task.VERIFY_COMMAND == list(task.DEFAULT_VERIFY_COMMAND)
    assert task.PROTECTED_PREFIXES == task.DEFAULT_PROTECTED_PREFIXES
    assert task.MAX_TOOL_ROUNDS == task.DEFAULT_MAX_TOOL_ROUNDS


def test_environment_overrides_rebind_runtime_config(task, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_PROMPT", "  从零写一个 psv_quota  ")
    monkeypatch.setenv("CODEGEN_REPO_NAME", "psv_quota")
    monkeypatch.setenv("CODEGEN_VERIFY", "bash scripts/test.sh")
    monkeypatch.setenv("CODEGEN_PROTECTED", "")
    monkeypatch.setenv("CODEGEN_MAX_ROUNDS", "80")

    assert task.load_job(tmp_path / "no-input") == "从零写一个 psv_quota"
    assert task.REPO_NAME == "psv_quota"
    assert task.VERIFY_COMMAND == ["bash", "scripts/test.sh"]
    assert task.PROTECTED_PREFIXES == ()
    assert task.MAX_TOOL_ROUNDS == 80


def test_empty_protected_prefixes_let_the_model_write_tests(task, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_PROTECTED", "")
    task.load_job(tmp_path / "no-input")
    task.prepare_workspace(EXAMPLE_REPO)
    assert task.write_file("tests/test_new.py", "def test_x(): pass\n").startswith("OK")
    # 路径越界的防线与受保护前缀无关, 不能一起放开。
    assert task.write_file("../escape.py", "x").startswith("ERROR")


def test_cloud_job_file_is_read_from_mounted_dataset(task, tmp_path):
    mounted = tmp_path / "input" / "psv-quota"
    mounted.mkdir(parents=True)
    (mounted / task.CODEGEN_JOB_FILE).write_text(
        json.dumps(
            {
                "prompt": "云端提示词",
                "repo_name": "psv_quota",
                "verify_command": ["bash", "scripts/test.sh"],
                "protected_prefixes": [],
                "max_tool_rounds": 60,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert task.load_job(tmp_path / "input") == "云端提示词"
    assert task.REPO_NAME == "psv_quota"
    assert task.VERIFY_COMMAND == ["bash", "scripts/test.sh"]
    assert task.PROTECTED_PREFIXES == ()
    assert task.MAX_TOOL_ROUNDS == 60


def test_environment_wins_over_cloud_job_file(task, monkeypatch, tmp_path):
    mounted = tmp_path / "input" / "psv-quota"
    mounted.mkdir(parents=True)
    (mounted / task.CODEGEN_JOB_FILE).write_text(
        json.dumps({"prompt": "云端", "max_tool_rounds": 60}), encoding="utf-8"
    )
    monkeypatch.setenv("CODEGEN_MAX_ROUNDS", "5")

    assert task.load_job(tmp_path / "input") == "云端"
    assert task.MAX_TOOL_ROUNDS == 5


@pytest.mark.parametrize(
    "variable, value",
    [
        ("CODEGEN_PROMPT", "   "),
        ("CODEGEN_VERIFY", ""),
        ("CODEGEN_MAX_ROUNDS", "0"),
        ("CODEGEN_MAX_ROUNDS", "不是数字"),
    ],
)
def test_bad_job_values_fail_loudly(task, monkeypatch, tmp_path, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError):
        task.load_job(tmp_path / "no-input")


def test_multiple_job_files_are_ambiguous(task, tmp_path):
    for name in ("a", "b"):
        directory = tmp_path / "input" / name
        directory.mkdir(parents=True)
        (directory / task.CODEGEN_JOB_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError):
        task.load_job(tmp_path / "input")


# ==== 工作副本的保留与续跑 ==================================================


def test_rerun_without_resume_starts_from_the_pristine_repository(task):
    task.prepare_workspace(EXAMPLE_REPO)
    task.write_file("textstats/wordcount.py", "# 上一轮的成果\n")
    task.prepare_workspace(EXAMPLE_REPO)
    assert "# 上一轮的成果" not in (task.WORKSPACE / "textstats" / "wordcount.py").read_text()


def test_resume_keeps_the_previous_round_of_work(task):
    task.prepare_workspace(EXAMPLE_REPO)
    task.write_file("textstats/wordcount.py", "# 上一轮的成果\n")
    task.write_file("textstats/added.py", "VALUE = 1\n")

    snapshot = task.prepare_workspace(EXAMPLE_REPO, resume=True)
    assert (task.WORKSPACE / "textstats" / "wordcount.py").read_text() == "# 上一轮的成果\n"
    assert (task.WORKSPACE / "textstats" / "added.py").exists()
    # 快照取自原始仓库, 所以 diff 是相对原始仓库的累计改动, 不是相对上一轮。
    assert snapshot["textstats/wordcount.py"] != "# 上一轮的成果\n"
    diff = task.build_diff(snapshot)
    assert "textstats/wordcount.py" in diff
    assert "textstats/added.py" in diff


def test_resume_falls_back_to_a_fresh_copy_when_nothing_is_there(task):
    snapshot = task.prepare_workspace(EXAMPLE_REPO, resume=True)
    assert (task.WORKSPACE / "textstats" / "wordcount.py").exists()
    assert task.build_diff(snapshot) == ""


def test_resume_ignores_a_workspace_holding_only_caches(task):
    task.prepare_workspace(EXAMPLE_REPO)
    task.execute_verification()  # 只留下 .pytest_cache
    for path in sorted(task.WORKSPACE.rglob("*")):
        if task.is_tracked(path):
            path.unlink()

    task.prepare_workspace(EXAMPLE_REPO, resume=True)
    assert (task.WORKSPACE / "textstats" / "wordcount.py").exists()
