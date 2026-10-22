"""Modal 后端：一个命名会话 = 一个 Modal GPU Sandbox。

- 会话 VM      → modal.Sandbox（`sleep infinity` 常驻，gpu=<规格>，timeout 上限 24h）
- Drive 模型盘 → modal.Volume 挂到 /content/drive/MyDrive/code/backup/models，
                 远端 remote_workflow.py 无需改路径即可复用模型缓存
- upload       → sandbox.filesystem.copy_from_local
- exec_*       → sandbox.exec("bash","-lc","python -u ... 2>&1")，逐行转事件
- 输出         → REMOTE_OUTPUT_NO_WAIT=1 运行后按 manifest 读分块，本地重组校验

会话名 → sandbox_id 的映射存在 ~/.colab_backends/modal/sessions.json，
同时给 Sandbox 打 tag `colab_session=<name>`，本地状态丢失时可 list 找回。

环境变量（全部可选）：
  MODAL_APP_NAME          默认 colab-task-list
  MODAL_MODELS_VOLUME     默认 colab-task-models（不存在会自动创建）
  MODAL_SANDBOX_TIMEOUT   秒，默认 21600（6h）；Modal 上限 86400
  MODAL_SECRETS           逗号分隔的 Modal Secret 名（如 hf-token），注入 Sandbox
  MODAL_COMFYUI_REF       镜像内 ComfyUI 的 git ref，默认 master
  MODAL_IMAGE             覆盖基础镜像（registry 镜像名），默认 debian_slim + 预装 ComfyUI
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any, Optional

from .base import EventSink, Transport, TransportError, _null_sink
from .batch import BATCH_ENV, fetch_manifest_output, forward_remote_line, state_root
from .constants import REMOTE_TRANSFER_DIR

DRIVE_MODELS_DIR = "/content/drive/MyDrive/code/backup/models"
REMOTE_SCRIPT_DIR = "/content/_backend"
SESSION_TAG = "colab_session"
DEFAULT_APP_NAME = "colab-task-list"
DEFAULT_VOLUME = "colab-task-models"
DEFAULT_TIMEOUT = 6 * 3600
MAX_TIMEOUT = 24 * 3600


def build_image(modal: Any):
    """预装 ComfyUI 与通用依赖的镜像（首次构建约 5-10 分钟，之后命中缓存）。"""
    custom = os.environ.get("MODAL_IMAGE")
    if custom:
        return modal.Image.from_registry(custom, add_python="3.11")
    ref = os.environ.get("MODAL_COMFYUI_REF", "master")
    return (
        modal.Image.debian_slim(python_version="3.11")
        .apt_install("git", "ffmpeg", "libgl1", "libglib2.0-0", "unzip")
        .run_commands(
            "git clone https://github.com/comfyanonymous/ComfyUI.git /content/ComfyUI",
            f"cd /content/ComfyUI && git checkout {shlex.quote(ref)}",
            "pip install -r /content/ComfyUI/requirements.txt",
            "pip install 'huggingface_hub[hf_transfer]' websocket-client psutil",
        )
    )


class ModalTransport(Transport):
    mode = "modal"
    backend = "modal"

    def __init__(self, sink: Optional[EventSink] = None, modal_module: Any = None,
                 state_dir: Optional[Path] = None) -> None:
        self._sink = sink or _null_sink
        self._modal_module = modal_module
        self._state_file = (state_dir or state_root("modal")) / "sessions.json"

    # ── 基础设施 ────────────────────────────────────────────
    @property
    def modal(self):
        if self._modal_module is None:
            try:
                import modal  # noqa: PLC0415
            except ImportError as error:
                raise TransportError(
                    "未安装 modal SDK：pip install modal，然后 modal token new"
                ) from error
            self._modal_module = modal
        return self._modal_module

    def _log(self, text: str) -> None:
        self._sink("log.line", "progress", text, {"stream": "stdout", "text": text})

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_file)

    def _sandbox(self, session: str):
        entry = self._load_state().get(session)
        if not entry:
            raise TransportError(f"Modal 会话 {session!r} 不存在（本地无记录）")
        try:
            return self.modal.Sandbox.from_id(entry["sandbox_id"])
        except Exception as error:
            raise TransportError(f"Modal Sandbox {entry['sandbox_id']} 不可访问: {error}") from error

    def _alive(self, sandbox) -> bool:
        try:
            return sandbox.poll() is None
        except Exception:
            return False

    # ── Transport 接口 ──────────────────────────────────────
    def create_session(self, session: str, kind: str, spec: str) -> None:
        if kind != "gpu":
            raise TransportError("Modal 后端只支持 GPU")
        modal = self.modal
        timeout = min(int(os.environ.get("MODAL_SANDBOX_TIMEOUT", DEFAULT_TIMEOUT)), MAX_TIMEOUT)
        app = modal.App.lookup(os.environ.get("MODAL_APP_NAME", DEFAULT_APP_NAME),
                               create_if_missing=True)
        volume = modal.Volume.from_name(
            os.environ.get("MODAL_MODELS_VOLUME", DEFAULT_VOLUME), create_if_missing=True
        )
        secrets = [modal.Secret.from_name(name.strip())
                   for name in os.environ.get("MODAL_SECRETS", "").split(",") if name.strip()]
        self._log(f">>> 创建 Modal Sandbox: gpu={spec}, timeout={timeout}s")
        try:
            sandbox = modal.Sandbox.create(
                "sleep", "infinity",
                app=app,
                image=build_image(modal),
                gpu=spec,
                timeout=timeout,
                volumes={DRIVE_MODELS_DIR: volume},
                secrets=secrets,
                tags={SESSION_TAG: session},
                workdir="/content",
            )
        except Exception as error:
            raise TransportError(f"创建 Modal Sandbox 失败: {error}") from error
        state = self._load_state()
        state[session] = {"sandbox_id": sandbox.object_id, "gpu": spec}
        self._save_state(state)
        self._log(f">>> Modal Sandbox 已就绪: {sandbox.object_id}")

    def mount_drive(self, session: str) -> bool:
        # Volume 已在创建时挂到 Drive 模型路径，这里只确认目录可见
        self._exec_text(session, f"mkdir -p {DRIVE_MODELS_DIR} && ls {DRIVE_MODELS_DIR} | head -5")
        return True

    def stop_session(self, session: str) -> None:
        state = self._load_state()
        entry = state.pop(session, None)
        if entry:
            try:
                self.modal.Sandbox.from_id(entry["sandbox_id"]).terminate()
            except Exception as error:  # 已结束/不存在都视为已停止
                self._log(f">>> 终止 Sandbox 时忽略错误: {error}")
        self._save_state(state)

    def session_status(self, session: str) -> str:
        entry = self._load_state().get(session)
        if not entry:
            raise TransportError(f"Modal 会话 {session!r} 不存在")
        sandbox = self._sandbox(session)
        alive = self._alive(sandbox)
        return (
            f"Session: {session}\nBackend: modal\nSandbox: {entry['sandbox_id']}\n"
            f"Variant: GPU\nAccelerator: {entry.get('gpu')}\n"
            f"Status: {'IDLE' if alive else 'TERMINATED'}"
        )

    def check_session_readable(self, session: str, *args, **kwargs) -> None:
        if not self._alive(self._sandbox(session)):
            raise TransportError(f"Modal 会话 {session!r} 的 Sandbox 已结束")

    def upload(self, session: str, local_path, remote_path: str) -> None:
        sandbox = self._sandbox(session)
        parent = str(Path(remote_path).parent).replace("\\", "/")
        sandbox.filesystem.make_directory(parent, create_parents=True)
        sandbox.filesystem.copy_from_local(str(local_path), remote_path)

    def _run(self, session: str, command: str, timeout: float,
             sink: EventSink, env: Optional[dict] = None) -> int:
        sandbox = self._sandbox(session)
        process = sandbox.exec("bash", "-lc", command, timeout=int(timeout), env=env)
        for line in process.stdout:
            forward_remote_line(line, sink)
        return process.wait()

    def _exec_text(self, session: str, command: str) -> None:
        self._run(session, command, 300, self._sink)

    def _push_script(self, session: str, script_path: Path) -> str:
        remote = f"{REMOTE_SCRIPT_DIR}/{Path(script_path).name}"
        self.upload(session, script_path, remote)
        return remote

    def exec_script(self, session: str, script_path: Path, timeout: float,
                    sink: EventSink = _null_sink) -> None:
        effective = sink if sink is not _null_sink else self._sink
        remote = self._push_script(session, script_path)
        code = self._run(session, f"python -u {shlex.quote(remote)} 2>&1", timeout,
                         effective, env=dict(BATCH_ENV))
        if code != 0:
            raise TransportError(f"Modal 执行 {Path(script_path).name} 失败 (exit {code})")

    def exec_workflow(self, session: str, script_path: Path, timeout: float,
                      output_zip: Path, sink: EventSink = _null_sink) -> bool:
        self.exec_script(session, script_path, timeout, sink)
        self.download_output(session, output_zip, sink)
        return True

    def _reader(self, session: str):
        sandbox = self._sandbox(session)

        def read(remote_path: str) -> bytes:
            try:
                return sandbox.filesystem.read_bytes(remote_path)
            except Exception as error:
                raise FileNotFoundError(remote_path) from error

        return read

    def remote_output_ready(self, session: str) -> bool | None:
        try:
            self._reader(session)(f"{REMOTE_TRANSFER_DIR}/manifest.json")
            return True
        except (FileNotFoundError, TransportError):
            return False

    def download_output(self, session: str, output_zip: Path,
                        sink: EventSink = _null_sink) -> None:
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        try:
            manifest = fetch_manifest_output(self._reader(session), output_zip)
        except FileNotFoundError as error:
            raise TransportError(f"Modal 远端没有输出清单: {error}") from error
        self._log(f">>> 已从 Modal 取回输出 {manifest['archive_size'] / 1024**2:.1f} MiB")
