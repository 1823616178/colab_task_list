"""kcloud local --executor modal: 在 Modal Sandbox 里跑任务(代码生成/验证), 模型仍走 Kaggle 额度。

为什么只换「执行器」: kllm/kcloud 的价值是 Kaggle Benchmarks 的免费模型额度, Modal 给不了;
但跑 verify(pytest、构建)的算力和隔离环境可以放到 Modal, 避免在本机执行模型写出的代码。

流程: 建 Sandbox(debian_slim + git + kaggle-benchmarks + pytest) → 写入任务文件与 repo 压缩包
→ 以 MODEL_PROXY_URL/MODEL_PROXY_API_KEY 直连 Kaggle 模型代理执行 → 打包 /work/out 拉回本地运行目录。
注意: 不能用本机 127.0.0.1 上的 kllm 网关(Modal 访问不到本机)。
"""

from __future__ import annotations

import io
import os
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

TASK_PACKAGES = ("kaggle-benchmarks", "pytest")
WORK = "/work"
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}


def _import_modal():
    try:
        import modal  # noqa: PLC0415
    except ImportError as error:
        raise RuntimeError("未安装 modal: pip install 'kaggle-tools[modal]', 然后 modal token new") from error
    return modal


def pack_directory(directory: Path) -> bytes:
    """把仓库打成 tar.gz(跳过 .git/venv/node_modules 等)。"""
    buffer = io.BytesIO()

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if set(Path(info.name).parts) & EXCLUDED_DIRS else info

    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(directory, arcname="repo", filter=keep)
    return buffer.getvalue()


def run_in_modal(task_file: Path, environment: dict[str, str], work_directory: Path,
                 repo: Path | None = None, timeout: int = 3600, gpu: str | None = None,
                 modal_module: Any = None, echo: Callable[[str], None] = print) -> int:
    modal = modal_module or _import_modal()
    app = modal.App.lookup(os.environ.get("KCLOUD_MODAL_APP", "kcloud"), create_if_missing=True)
    image = (modal.Image.debian_slim(python_version="3.11")
             .apt_install("git").pip_install(*TASK_PACKAGES))
    sandbox = modal.Sandbox.create("sleep", "infinity", app=app, image=image,
                                   timeout=timeout + 600, gpu=gpu, workdir=WORK)
    try:
        sandbox.filesystem.make_directory(f"{WORK}/out", create_parents=True)
        sandbox.filesystem.copy_from_local(str(task_file), f"{WORK}/task.py")
        env = dict(environment)
        if repo is not None:
            sandbox.filesystem.write_bytes(pack_directory(repo), f"{WORK}/repo.tgz")
            sandbox.exec("tar", "xzf", f"{WORK}/repo.tgz", "-C", WORK).wait()
            env["CODEGEN_REPO_PATH"] = f"{WORK}/repo"
        for key in ("SPEC_PATH",):
            if key in env and Path(env[key]).is_file():
                sandbox.filesystem.copy_from_local(env[key], f"{WORK}/spec.md")
                env[key] = f"{WORK}/spec.md"
        echo(f"Modal Sandbox: {sandbox.object_id}")
        process = sandbox.exec("bash", "-lc", f"cd {WORK}/out && python -u {WORK}/task.py 2>&1",
                               env=env, timeout=timeout)
        for line in process.stdout:
            echo(line.rstrip("\n"))
        exit_code = process.wait()
        sandbox.exec("tar", "czf", f"{WORK}/out.tgz", "-C", f"{WORK}/out", ".").wait()
        data = sandbox.filesystem.read_bytes(f"{WORK}/out.tgz")
        work_directory.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            archive.extractall(work_directory, filter="data")
        return exit_code
    finally:
        try:
            sandbox.terminate()
        except Exception:  # noqa: BLE001 - 结束时尽力回收
            pass
