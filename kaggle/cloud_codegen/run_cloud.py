"""评测任务的驱动: 本地试跑 -> 打包数据集 -> push -> run -> 下载产物。

先用 local 把任务调通再 push: Kaggle 服务端不支持删除 task
(kaggle_api_extended.py:10329 的 delete 是空壳, SDK 里也没有 delete 端点),
每次 push 都是永久记录。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "anthropic/claude-sonnet-5@default"
# kaggle_benchmarks 自己会补 /openapi, 这里要给不带后缀的原始地址。
OPENAI_PATH_SUFFIX = "/openapi"

# 两个任务: repo 是仓库改造(带工具的 agent), codegen 是单函数生成。
TASKS = {
    "repo": ("repo-codegen", HERE / "repo_task.py"),
    "codegen": ("cloud-codegen", HERE / "codegen_task.py"),
}


def task_of(args: argparse.Namespace) -> tuple[str, Path]:
    """取出当前选中的任务 slug 与源文件。"""
    return TASKS[args.task]


def build_proxy_environment() -> dict[str, str]:
    """复用 kllm 的凭据管理, 组出本地跑任务所需的环境变量。"""
    sys.path.insert(0, str(HERE.parent))
    from kllm.config import load_settings
    from kllm.credentials import TokenManager

    credentials = TokenManager(load_settings()).get()
    base_url = credentials.base_url
    if base_url.endswith(OPENAI_PATH_SUFFIX):
        base_url = base_url[: -len(OPENAI_PATH_SUFFIX)]
    return {
        "MODEL_PROXY_URL": base_url,
        "MODEL_PROXY_API_KEY": credentials.token,
        "LLM_DEFAULT": os.environ.get("LLM_DEFAULT", DEFAULT_MODEL),
        "LLM_DEFAULT_EVAL": os.environ.get("LLM_DEFAULT_EVAL", DEFAULT_MODEL),
    }


def kaggle_command(arguments: list[str]) -> int:
    """执行一条 kaggle 命令并透传输出。"""
    print(f"$ kaggle {' '.join(arguments)}", flush=True)
    return subprocess.run(["kaggle", *arguments]).returncode


def command_local(args: argparse.Namespace) -> int:
    """在本地跑任务(算力是自己的, 只花模型额度), 用于 push 前调通。"""
    _, task_file = task_of(args)
    environment = dict(os.environ)
    environment.update(build_proxy_environment())
    if args.model:
        environment["LLM_DEFAULT"] = args.model
    if args.repo:
        environment["CODEGEN_REPO_PATH"] = str(Path(args.repo).resolve())

    work_directory = Path(args.output).resolve()
    work_directory.mkdir(parents=True, exist_ok=True)
    print(f"任务: {task_file.name}")
    print(f"模型: {environment['LLM_DEFAULT']}")
    print(f"产物: {work_directory}\n")
    return subprocess.run(
        [sys.executable, str(task_file)], env=environment, cwd=work_directory
    ).returncode


def command_dataset(args: argparse.Namespace) -> int:
    """把仓库打包成 Kaggle 私有数据集, 供云端任务通过 -d 挂载。"""
    repository = Path(args.repo).resolve()
    if not repository.is_dir():
        print(f"仓库目录不存在: {repository}", file=sys.stderr)
        return 1

    owner = args.owner or subprocess.run(
        ["kaggle", "config", "view"], capture_output=True, text=True
    ).stdout
    if not args.owner:
        owner = next(
            (line.split(":", 1)[1].strip() for line in owner.splitlines() if "username:" in line),
            "",
        )
    if not owner:
        print("拿不到用户名, 用 --owner 指定。", file=sys.stderr)
        return 1

    slug = args.slug or repository.name.replace("_", "-")
    staging = Path(args.staging).resolve()
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(
        repository,
        staging / repository.name,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "*.pyc"),
    )
    (staging / "dataset-metadata.json").write_text(
        json.dumps({"title": slug, "id": f"{owner}/{slug}", "licenses": [{"name": "CC0-1.0"}]},
                   indent=2)
    )

    print(f"⚠ 这会把 {repository} 的内容上传到 Kaggle(私有数据集 {owner}/{slug})。")
    if not args.yes:
        if input("确认上传? [y/N] ").strip().lower() != "y":
            print("已取消。")
            return 1

    action = ["datasets", "version", "-p", str(staging), "-m", args.message] if args.update \
        else ["datasets", "create", "-p", str(staging)]
    exit_code = kaggle_command(action)
    if exit_code == 0:
        print(f"\n挂载用: python run_cloud.py push -d {owner}/{slug}")
    return exit_code


def command_push(args: argparse.Namespace) -> int:
    """把任务推到 Kaggle。注意: 不可删除, 不可撤销。"""
    slug, task_file = task_of(args)
    print(f"⚠ push 会在 Kaggle 上创建永久记录(源码 + backing notebook), 服务端不支持删除。")
    print(f"  任务: {slug}   文件: {task_file.name}")
    if args.dataset:
        print(f"  挂载数据集: {', '.join(args.dataset)}")
    if not args.yes:
        if input(f"确认推送? [y/N] ").strip().lower() != "y":
            print("已取消。")
            return 1

    arguments = ["b", "t", "push", slug, "-f", str(task_file), "--wait", "600"]
    for dataset in args.dataset:
        arguments += ["-d", dataset]
    return kaggle_command(arguments)


def command_run(args: argparse.Namespace) -> int:
    """在云端跑一次任务。"""
    slug, _ = task_of(args)
    arguments = ["b", "t", "run", slug, "-m", args.model]
    if args.wait:
        arguments += ["--wait", str(args.wait)]
    return kaggle_command(arguments)


def command_status(args: argparse.Namespace) -> int:
    """查看任务与各模型的运行状态。"""
    return kaggle_command(["b", "t", "status", task_of(args)[0]])


def command_log(args: argparse.Namespace) -> int:
    """拉取云端运行日志。"""
    return kaggle_command(["b", "t", "log", task_of(args)[0], "-m", args.model])


def command_download(args: argparse.Namespace) -> int:
    """下载 run 产物并解压。"""
    output = Path(args.output).resolve()
    exit_code = kaggle_command(
        ["b", "t", "download", task_of(args)[0], "-m", args.model, "-p", str(output)]
    )
    if exit_code != 0:
        return exit_code
    for archive in sorted(output.rglob("*.zip")):
        target = archive.with_suffix("")
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(target)
        print(f"解压 {archive.name} -> {target}")
    print("\n下载到的文件:")
    for path in sorted(output.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(output)}  ({path.stat().st_size} B)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(prog="run_cloud", description="Kaggle 评测任务驱动")
    parser.add_argument("-t", "--task", choices=sorted(TASKS), default="repo",
                        help="选择任务(默认 repo: 仓库改造)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    local_parser = subparsers.add_parser("local", help="本地试跑(push 前必做)")
    local_parser.add_argument("-m", "--model", default=None)
    local_parser.add_argument("-r", "--repo", default=None, help="仓库路径(repo 任务用)")
    local_parser.add_argument("-o", "--output", default="./local_output")
    local_parser.set_defaults(handler=command_local)

    dataset_parser = subparsers.add_parser("dataset", help="把仓库上传为私有数据集")
    dataset_parser.add_argument("-r", "--repo", required=True)
    dataset_parser.add_argument("--slug", default=None)
    dataset_parser.add_argument("--owner", default=None)
    dataset_parser.add_argument("--staging", default="./dataset_staging")
    dataset_parser.add_argument("--update", action="store_true", help="更新已存在的数据集")
    dataset_parser.add_argument("-m", "--message", default="update")
    dataset_parser.add_argument("-y", "--yes", action="store_true")
    dataset_parser.set_defaults(handler=command_dataset)

    push_parser = subparsers.add_parser("push", help="推送任务(永久, 不可删除)")
    push_parser.add_argument("-d", "--dataset", action="append", default=[],
                             help="挂载数据集 owner/slug, 可重复")
    push_parser.add_argument("-y", "--yes", action="store_true")
    push_parser.set_defaults(handler=command_push)

    run_parser = subparsers.add_parser("run", help="在云端运行")
    run_parser.add_argument("-m", "--model", default=DEFAULT_MODEL)
    run_parser.add_argument("--wait", type=int, default=900)
    run_parser.set_defaults(handler=command_run)

    status_parser = subparsers.add_parser("status", help="查看状态")
    status_parser.set_defaults(handler=command_status)

    log_parser = subparsers.add_parser("log", help="查看云端日志")
    log_parser.add_argument("-m", "--model", default=DEFAULT_MODEL)
    log_parser.set_defaults(handler=command_log)

    download_parser = subparsers.add_parser("download", help="下载并解压产物")
    download_parser.add_argument("-m", "--model", default=DEFAULT_MODEL)
    download_parser.add_argument("-o", "--output", default="./cloud_output")
    download_parser.set_defaults(handler=command_download)

    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行主入口。"""
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
