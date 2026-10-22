"""命令行参数、任务路径和本地文件输入的回归测试。"""

from argparse import Namespace
from pathlib import Path

import pytest

from kaggle_tools.cloud import cli as run_cloud


def test_task_files_are_grouped_under_tasks_directory():
    for _, task_file in run_cloud.TASKS.values():
        assert task_file.parent == run_cloud.TASK_DIRECTORY
        assert task_file.is_file()


def test_prompt_file_is_read_as_utf8(tmp_path):
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("  修复测试\n", encoding="utf-8")
    args = Namespace(prompt=None, prompt_file=str(prompt_file))
    assert run_cloud.prompt_of(args) == "修复测试"


def test_empty_prompt_is_rejected():
    with pytest.raises(ValueError, match="提示词不能为空"):
        run_cloud.prompt_of(Namespace(prompt="  ", prompt_file=None))


def test_prompt_arguments_are_mutually_exclusive():
    parser = run_cloud.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["local", "-p", "one", "--prompt-file", "two.md"])


def test_generated_files_default_to_runs_directory():
    parser = run_cloud.build_parser()
    local = parser.parse_args(["local"])
    dataset = parser.parse_args(["dataset", "--repo", "."])
    download = parser.parse_args(["download"])

    assert Path(local.output).parent == run_cloud.RUNS_DIRECTORY
    assert Path(dataset.staging).parent == run_cloud.RUNS_DIRECTORY
    assert Path(download.output).parent == run_cloud.RUNS_DIRECTORY


@pytest.mark.parametrize("staging_name", ["same", "child"])
def test_dataset_rejects_staging_that_overlaps_repository(tmp_path, staging_name):
    repository = tmp_path / "repository"
    repository.mkdir()
    staging = repository if staging_name == "same" else repository / "staging"
    args = run_cloud.build_parser().parse_args(
        [
            "dataset",
            "--repo",
            str(repository),
            "--owner",
            "example-user",
            "--staging",
            str(staging),
            "--yes",
        ]
    )

    assert args.handler(args) == 1
