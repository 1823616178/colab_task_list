"""Kaggle 后端：把一次任务打包成私有 Kaggle Notebook（script kernel）跑在 Kaggle GPU 上。

Kaggle 没有可交互的会话、也不能从外部连入，所以这是「文件进、文件出」的批处理：

- create_session / upload / exec_script 只在本地暂存（~/.colab_backends/kaggle/<会话>/）
- exec_workflow 时把暂存的文件打成 bundle：
    * 小于 KAGGLE_INLINE_MAX_BYTES（默认 1.5 MB）→ base64 内嵌进 kernel 脚本
    * 更大 → 推成私有 Dataset `<用户>/<slug>-inputs` 并挂到 kernel
  然后 `kaggle kernels push`（--accelerator 选 GPU）→ 轮询 `kernels status`
  → `kernels output` 下载 → 按 output_transfer/manifest.json 重组校验 output.zip
- Drive 模型盘 → KAGGLE_MODELS_DATASET（如 you/comfy-models）挂到 /kaggle/input，
  逐个软链到 /content/drive/MyDrive/code/backup/models（目录本身可写，新下载的模型不报错）

kaggle 命令优先复用 kaggle_tools.common.kaggle_cli.run_kaggle（带超时与友好报错），
未安装 kaggle_tools 时退回直接调用 `kaggle`。

环境变量：
  KAGGLE_USERNAME / KAGGLE_KEY（或 ~/.kaggle/kaggle.json）  Kaggle 凭据
  KAGGLE_MODELS_DATASET     可选，模型 Dataset slug
  KAGGLE_INLINE_MAX_BYTES   可选，内嵌上限
  KAGGLE_POLL_SECONDS       可选，状态轮询间隔，默认 30
  KAGGLE_ENABLE_INTERNET    默认 1（ComfyUI 需 pip/HF 下载；账号需手机验证）
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

from .base import EventSink, Transport, TransportError, _null_sink
from .batch import fetch_manifest_output, forward_remote_line, state_root

DRIVE_MODELS_DIR = "/content/drive/MyDrive/code/backup/models"
REMOTE_SCRIPT_DIR = "/content/_backend"
KAGGLE_GPU_MACHINES = {"T4": "NvidiaTeslaT4", "T4x2": "NvidiaTeslaT4", "P100": "NvidiaTeslaP100"}
DEFAULT_INLINE_MAX = 1_500_000
TERMINAL_OK = ("complete",)
TERMINAL_FAIL = ("error", "cancel")

KERNEL_TEMPLATE = r'''# 由 colab_task_list 的 Kaggle 后端自动生成，请勿手改
import base64, glob, io, json, os, shutil, subprocess, sys, zipfile

INLINE_BUNDLE = {inline!r}
MODELS_DATASET = {models!r}
SCRIPTS = {scripts!r}
DRIVE_MODELS_DIR = {drive!r}


def bundle_bytes():
    if INLINE_BUNDLE:
        return base64.b64decode(INLINE_BUNDLE)
    hits = glob.glob("/kaggle/input/**/bundle.bin", recursive=True)
    if not hits:
        raise SystemExit("找不到输入 bundle.bin（Dataset 可能尚未就绪）")
    with open(hits[0], "rb") as handle:
        return handle.read()


with zipfile.ZipFile(io.BytesIO(bundle_bytes())) as archive:
    archive.extractall("/")

os.makedirs(DRIVE_MODELS_DIR, exist_ok=True)
if MODELS_DATASET:
    roots = [p for p in glob.glob("/kaggle/input/*") if os.path.basename(p) == MODELS_DATASET.split("/")[-1]]
    roots = roots or glob.glob("/kaggle/input/datasets/*/" + MODELS_DATASET.split("/")[-1])
    for root in roots:
        for name in os.listdir(root):
            target = os.path.join(DRIVE_MODELS_DIR, name)
            if not os.path.exists(target):
                os.symlink(os.path.join(root, name), target)
        print(">>> 已挂载模型 Dataset:", root, flush=True)

env = dict(os.environ, REMOTE_OUTPUT_NO_WAIT="1", PYTHONUNBUFFERED="1")
code = 0
for script in SCRIPTS:
    print(">>> 运行", script, flush=True)
    code = subprocess.call([sys.executable, "-u", script], cwd="/content", env=env)
    if code != 0:
        print(">>> 脚本失败:", script, "exit", code, flush=True)
        break

if os.path.isdir("/content/output_transfer"):
    shutil.copytree("/content/output_transfer", "/kaggle/working/output_transfer", dirs_exist_ok=True)
with open("/kaggle/working/backend_result.json", "w") as handle:
    json.dump({{"exit_code": code}}, handle)
'''


def _default_runner(arguments: list[str], timeout: float = 1800.0) -> subprocess.CompletedProcess:
    try:
        from kaggle_tools.common.kaggle_cli import run_kaggle  # noqa: PLC0415
    except ImportError:
        run_kaggle = None
    if run_kaggle is not None:
        return run_kaggle(arguments, capture=True, timeout=timeout)
    exe = shutil.which("kaggle")
    if exe is None:
        raise TransportError("找不到 kaggle 命令：pip install kaggle 并配置 ~/.kaggle/kaggle.json")
    return subprocess.run([exe, *arguments], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def kernel_slug(session: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", f"colab-{session}".lower()).strip("-")
    return slug[:50] or "colab-task"


def parse_status(text: str) -> str:
    """`kaggle kernels status` 输出 → queued | running | complete | error | cancel | unknown。"""
    lowered = text.lower()
    for key in ("complete", "error", "cancel", "running", "queued"):
        if key in lowered:
            return key
    return "unknown"


class KaggleTransport(Transport):
    mode = "kaggle"
    backend = "kaggle"

    def __init__(self, sink: Optional[EventSink] = None,
                 runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
                 state_dir: Optional[Path] = None, sleep: Callable[[float], None] = time.sleep,
                 username: Optional[str] = None) -> None:
        self._sink = sink or _null_sink
        self._runner = runner or _default_runner
        self._root = state_dir or state_root("kaggle")
        self._sleep = sleep
        self._username = username

    # ── 本地暂存 ───────────────────────────────────────────
    def _dir(self, session: str) -> Path:
        return self._root / kernel_slug(session)

    def _plan_path(self, session: str) -> Path:
        return self._dir(session) / "plan.json"

    def _plan(self, session: str) -> dict:
        try:
            return json.loads(self._plan_path(session).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise TransportError(f"Kaggle 会话 {session!r} 不存在（未创建或已停止）") from error

    def _save(self, session: str, plan: dict) -> None:
        self._plan_path(session).write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                                            encoding="utf-8")

    def _log(self, text: str) -> None:
        self._sink("log.line", "progress", text, {"stream": "stdout", "text": text})

    def _kaggle(self, *arguments: str, timeout: float = 1800.0, check: bool = True):
        result = self._runner(list(arguments), timeout=timeout)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-800:]
            raise TransportError(f"kaggle {' '.join(arguments[:2])} 失败 (exit {result.returncode}): {detail}")
        return result

    def _owner(self) -> str:
        if self._username:
            return self._username
        name = os.environ.get("KAGGLE_USERNAME")
        if not name:
            try:
                from kaggle_tools.common.kaggle_cli import current_username  # noqa: PLC0415

                name = current_username()
            except Exception as error:
                raise TransportError("无法确定 Kaggle 用户名：请设置 KAGGLE_USERNAME") from error
        self._username = name
        return name

    # ── Transport 接口 ─────────────────────────────────────
    def create_session(self, session: str, kind: str, spec: str) -> None:
        if kind != "gpu" or spec not in KAGGLE_GPU_MACHINES:
            raise TransportError(f"Kaggle 只支持 GPU {', '.join(KAGGLE_GPU_MACHINES)}，收到 {kind}/{spec}")
        directory = self._dir(session)
        shutil.rmtree(directory, ignore_errors=True)
        (directory / "files").mkdir(parents=True)
        self._save(session, {"session": session, "spec": spec, "scripts": [],
                             "models_dataset": None, "kernel": None})
        self._log(f">>> Kaggle 会话已在本地暂存（批处理，提交时才占用 GPU 配额）: {directory}")

    def mount_drive(self, session: str) -> bool:
        plan = self._plan(session)
        dataset = os.environ.get("KAGGLE_MODELS_DATASET")
        plan["models_dataset"] = dataset or None
        self._save(session, plan)
        if not dataset:
            self._log(">>> 未设置 KAGGLE_MODELS_DATASET：模型将按工作流配置在 Kaggle 上在线下载")
            return False
        self._log(f">>> 模型 Dataset {dataset} 将挂载到 {DRIVE_MODELS_DIR}")
        return True

    def stop_session(self, session: str) -> None:
        shutil.rmtree(self._dir(session), ignore_errors=True)

    def session_status(self, session: str) -> str:
        plan = self._plan(session)
        state = "IDLE"
        if plan.get("kernel"):
            result = self._kaggle("kernels", "status", plan["kernel"], timeout=120, check=False)
            if parse_status(result.stdout or "") in ("running", "queued"):
                state = "BUSY"
        return (f"Session: {session}\nBackend: kaggle\nKernel: {plan.get('kernel') or '-'}\n"
                f"Variant: GPU\nAccelerator: {plan['spec']}\nStatus: {state}")

    def check_session_readable(self, session: str, *args, **kwargs) -> None:
        self._plan(session)

    def upload(self, session: str, local_path, remote_path: str) -> None:
        self._plan(session)
        target = self._dir(session) / "files" / remote_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, target)

    def exec_script(self, session: str, script_path: Path, timeout: float,
                    sink: EventSink = _null_sink) -> None:
        plan = self._plan(session)
        remote = f"{REMOTE_SCRIPT_DIR}/{Path(script_path).name}"
        self.upload(session, script_path, remote)
        if remote not in plan["scripts"]:
            plan["scripts"].append(remote)
        self._save(session, plan)
        self._log(f">>> 已排入 Kaggle kernel: {Path(script_path).name}（与工作流一起在 Kaggle 上执行）")

    def _bundle(self, session: str) -> bytes:
        buffer = io.BytesIO()
        files = self._dir(session) / "files"
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(files.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(files).as_posix())
        return buffer.getvalue()

    def _push_inputs_dataset(self, session: str, bundle: bytes) -> str:
        ref = f"{self._owner()}/{kernel_slug(session)}-inputs"
        directory = self._dir(session) / "dataset"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir()
        (directory / "bundle.bin").write_bytes(bundle)
        (directory / "dataset-metadata.json").write_text(json.dumps(
            {"title": f"{kernel_slug(session)}-inputs", "id": ref,
             "licenses": [{"name": "CC0-1.0"}]}), encoding="utf-8")
        self._log(f">>> 输入 {len(bundle) / 1024**2:.1f} MiB 超过内嵌上限，推送私有 Dataset {ref}")
        result = self._kaggle("datasets", "version", "-p", str(directory), "-m", "colab_task_list inputs",
                              check=False)
        if result.returncode != 0:
            self._kaggle("datasets", "create", "-p", str(directory))
        return ref

    def build_kernel_dir(self, session: str) -> Path:
        plan = self._plan(session)
        bundle = self._bundle(session)
        limit = int(os.environ.get("KAGGLE_INLINE_MAX_BYTES", DEFAULT_INLINE_MAX))
        sources = []
        inline = ""
        if len(bundle) <= limit:
            inline = base64.b64encode(bundle).decode("ascii")
        else:
            sources.append(self._push_inputs_dataset(session, bundle))
        if plan.get("models_dataset"):
            sources.append(plan["models_dataset"])
        directory = self._dir(session) / "kernel"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir()
        (directory / "run.py").write_text(KERNEL_TEMPLATE.format(
            inline=inline, models=plan.get("models_dataset") or "",
            scripts=plan["scripts"], drive=DRIVE_MODELS_DIR), encoding="utf-8")
        slug = kernel_slug(session)
        (directory / "kernel-metadata.json").write_text(json.dumps({
            "id": f"{self._owner()}/{slug}", "title": slug, "code_file": "run.py",
            "language": "python", "kernel_type": "script", "is_private": True,
            "enable_gpu": True, "enable_tpu": False,
            "enable_internet": os.environ.get("KAGGLE_ENABLE_INTERNET", "1") != "0",
            "dataset_sources": sources, "competition_sources": [], "kernel_sources": [],
            "model_sources": [],
        }, indent=2), encoding="utf-8")
        return directory

    def exec_workflow(self, session: str, script_path: Path, timeout: float,
                      output_zip: Path, sink: EventSink = _null_sink) -> bool:
        effective = sink if sink is not _null_sink else self._sink
        self.exec_script(session, script_path, timeout, sink)
        plan = self._plan(session)
        kernel_dir = self.build_kernel_dir(session)
        ref = f"{self._owner()}/{kernel_slug(session)}"
        self._log(f">>> 推送 Kaggle kernel {ref}（GPU {plan['spec']}）")
        self._kaggle("kernels", "push", "-p", str(kernel_dir),
                     "--accelerator", KAGGLE_GPU_MACHINES[plan["spec"]], "--timeout", str(int(timeout)))
        plan["kernel"] = ref
        self._save(session, plan)

        interval = float(os.environ.get("KAGGLE_POLL_SECONDS", 30))
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        status = "unknown"
        while time.monotonic() < deadline:
            result = self._kaggle("kernels", "status", ref, timeout=120, check=False)
            status = parse_status((result.stdout or "") + (result.stderr or ""))
            effective("workflow.exec", "progress", f">>> Kaggle kernel 状态: {status}",
                      {"reason": "kaggle_poll", "kernel": ref, "kernel_status": status,
                       "elapsed_seconds": round(time.monotonic() - started)})
            if status in TERMINAL_OK or status in TERMINAL_FAIL:
                break
            self._sleep(interval)
        else:
            raise TransportError(f"Kaggle kernel {ref} 在 {timeout:.0f}s 内未结束（最后状态 {status}）")

        result_info = self._fetch(session, output_zip, effective, need_output=status in TERMINAL_OK)
        if status in TERMINAL_FAIL or result_info.get("exit_code", 0) != 0:
            raise TransportError(
                f"Kaggle kernel {ref} 失败（状态 {status}, exit {result_info.get('exit_code')}），"
                f"日志见 https://www.kaggle.com/code/{ref}"
            )
        return True

    def _fetch(self, session: str, output_zip: Path, sink: EventSink,
               need_output: bool = True) -> dict:
        plan = self._plan(session)
        if not plan.get("kernel"):
            raise TransportError(f"Kaggle 会话 {session!r} 还没有运行过 kernel")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._kaggle("kernels", "output", plan["kernel"], "-p", str(root), "-o")
            for log_file in root.glob("*.log"):
                try:
                    entries = json.loads(log_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    entries = []
                for entry in entries if isinstance(entries, list) else []:
                    for line in str(entry.get("data", "")).splitlines():
                        forward_remote_line(line, sink)
            info_path = next(iter(root.rglob("backend_result.json")), None)
            info = json.loads(info_path.read_text()) if info_path else {}

            def read(remote_path: str) -> bytes:
                name = Path(remote_path).name
                hit = next((p for p in root.rglob(name) if p.parent.name == "output_transfer"), None)
                if hit is None:
                    raise FileNotFoundError(remote_path)
                return hit.read_bytes()

            if need_output and info.get("exit_code", 0) == 0:
                output_zip.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fetch_manifest_output(read, output_zip)
                except FileNotFoundError as error:
                    raise TransportError(f"Kaggle 输出中没有 output_transfer 清单: {error}") from error
        return info

    def remote_output_ready(self, session: str) -> bool | None:
        plan = self._plan(session)
        if not plan.get("kernel"):
            return False
        result = self._kaggle("kernels", "status", plan["kernel"], timeout=120, check=False)
        return parse_status(result.stdout or "") == "complete"

    def download_output(self, session: str, output_zip: Path,
                        sink: EventSink = _null_sink) -> None:
        self._fetch(session, output_zip, sink if sink is not _null_sink else self._sink)
