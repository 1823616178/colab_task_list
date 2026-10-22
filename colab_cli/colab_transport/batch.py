"""批处理型后端（Kaggle / Modal）共用的工具。

两者都没有 Colab 那样的「远端等待本地 ACK」握手：远端脚本以
REMOTE_OUTPUT_NO_WAIT=1 运行，写完 output_transfer/manifest.json + 分块后
直接退出，本地再按清单读取分块并重组校验（复用 transfer.assemble_output_archive）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

from .base import EventSink
from .constants import (
    OUTPUT_STREAM_PREFIX,
    REMOTE_TRANSFER_DIR,
    STREAM_PAYLOAD_METRICS,
    STREAM_PAYLOAD_PROGRESS,
)
from .transfer import assemble_output_archive, load_transfer_manifest

BATCH_ENV = {"REMOTE_OUTPUT_NO_WAIT": "1", "PYTHONUNBUFFERED": "1"}


def state_root(backend: str) -> Path:
    """本地状态目录：COLAB_BACKEND_STATE_DIR 或 ~/.colab_backends/<backend>。"""
    base = os.environ.get("COLAB_BACKEND_STATE_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".colab_backends"
    path = root / backend
    path.mkdir(parents=True, exist_ok=True)
    return path


def forward_remote_line(line: str, sink: EventSink) -> None:
    """把远端一行 stdout 转成事件：PROGRESS/METRICS → 结构化事件，其余 → log.line。

    Colab 专用的输出流协议行（READY/BEGIN/CHUNK/END）在批处理后端不需要，丢弃。
    """
    line = line.rstrip("\r\n")
    if not line.startswith(OUTPUT_STREAM_PREFIX):
        sink("log.line", "progress", line, {"stream": "stdout", "text": line})
        return
    payload = line[len(OUTPUT_STREAM_PREFIX):]
    for prefix, phase in (
        (STREAM_PAYLOAD_PROGRESS, "workflow.exec"),
        (STREAM_PAYLOAD_METRICS, "metrics.sample"),
    ):
        if payload.startswith(prefix):
            text = payload[len(prefix):]
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                data = None
            if isinstance(data, dict):
                sink(phase, "progress", data.get("text", ""), data)
            else:
                sink("log.line", "progress", text, {"stream": "stdout", "text": text})
            return


def fetch_manifest_output(
    read_file: Callable[[str], bytes],
    output_zip: Path,
    transfer_dir: str = REMOTE_TRANSFER_DIR,
) -> dict:
    """按远端清单读取全部分块，重组并原子写出 output_zip；返回清单。

    read_file(remote_path) -> bytes；缺失时应抛 FileNotFoundError。
    """
    workdir = Path(tempfile.mkdtemp(prefix=".batch_output_", dir=output_zip.parent))
    try:
        manifest_path = workdir / "manifest.json"
        manifest_path.write_bytes(read_file(f"{transfer_dir}/manifest.json"))
        manifest = load_transfer_manifest(manifest_path)
        parts_dir = workdir / "parts"
        parts_dir.mkdir()
        for part in manifest["parts"]:
            (parts_dir / part["name"]).write_bytes(
                read_file(f"{transfer_dir}/{part['name']}")
            )
        assemble_output_archive(manifest, parts_dir, output_zip)
        return manifest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def local_dir_reader(root: Path, transfer_dir: str = REMOTE_TRANSFER_DIR,
                     local_subdir: Optional[str] = None) -> Callable[[str], bytes]:
    """把「远端路径」映射到本地已下载目录（Kaggle kernels output 用）。"""
    sub = Path(local_subdir) if local_subdir else Path(Path(transfer_dir).name)

    def read(remote_path: str) -> bytes:
        name = Path(remote_path).name
        candidate = root / sub / name
        if not candidate.is_file():
            raise FileNotFoundError(str(candidate))
        return candidate.read_bytes()

    return read
