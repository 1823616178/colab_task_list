"""输出取回与校验的共享工具（自 run.py 迁入，逻辑不变）。

download_with_retries / contents_download_with_retries 分别服务于
CliTransport（subprocess colab download）与 NativeTransport（Contents API）。
重试间隔等常量读自本包 constants 模块；测试通过 monkeypatch
`colab_transfer.constants` 调整。
"""
import hashlib
import json
import os
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from . import constants
from .constants import (
    DOWNLOAD_MAX_ATTEMPTS,
)


def file_sha256(path: Path) -> str:
    """计算本地文件的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def valid_sha256(value) -> bool:
    """判断一个值是否为完整的十六进制 SHA-256 字符串。"""
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def load_transfer_manifest(path: Path) -> dict:
    """读取并严格验证远端输出分块清单。"""
    with path.open(encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("输出分块清单版本无效")

    archive_size = manifest.get("archive_size")
    archive_sha256 = manifest.get("archive_sha256")
    parts = manifest.get("parts")
    if not isinstance(archive_size, int) or archive_size < 0:
        raise ValueError("输出分块清单的 archive_size 无效")
    if not valid_sha256(archive_sha256):
        raise ValueError("输出分块清单的 archive_sha256 无效")
    if not isinstance(parts, list) or not parts:
        raise ValueError("输出分块清单没有可下载的分块")

    part_names = set()
    part_size_total = 0
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("输出分块清单中的分块必须是对象")
        name = part.get("name")
        size = part.get("size")
        sha256 = part.get("sha256")
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError(f"输出分块名称无效: {name!r}")
        if name in part_names:
            raise ValueError(f"输出分块名称重复: {name}")
        part_names.add(name)
        if not isinstance(size, int) or size <= 0:
            raise ValueError(f"输出分块大小无效: {name}")
        part_size_total += size
        if not valid_sha256(sha256):
            raise ValueError(f"输出分块 SHA-256 无效: {name}")
    if part_size_total != archive_size:
        raise ValueError("输出分块总大小与 archive_size 不一致")
    return manifest


def assemble_output_archive(
    manifest: dict,
    parts_dir: Path,
    output_zip: Path,
) -> None:
    """顺序重组分块，并原子提交经过哈希和 ZIP CRC 校验的结果。"""
    temporary_output = output_zip.with_name(f".{output_zip.name}.assembling")
    temporary_output.unlink(missing_ok=True)
    digest = hashlib.sha256()
    total_size = 0
    try:
        with temporary_output.open("wb") as destination:
            for part in manifest["parts"]:
                with (parts_dir / part["name"]).open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
                        digest.update(chunk)
                        total_size += len(chunk)
            destination.flush()
            os.fsync(destination.fileno())

        if total_size != manifest["archive_size"]:
            raise RuntimeError("重组后的输出 ZIP 大小与远端清单不一致")
        if digest.hexdigest() != manifest["archive_sha256"].lower():
            raise RuntimeError("重组后的输出 ZIP SHA-256 与远端清单不一致")
        try:
            with zipfile.ZipFile(temporary_output) as archive:
                corrupt_member = archive.testzip()
        except zipfile.BadZipFile as error:
            raise RuntimeError("重组后的输出不是有效 ZIP") from error
        if corrupt_member is not None:
            raise RuntimeError(f"输出 ZIP 中的文件已损坏: {corrupt_member}")
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        temporary_output.replace(output_zip)
    except Exception:
        temporary_output.unlink(missing_ok=True)
        raise


def upload_contents_signal(contents_client, remote_path: str) -> None:
    """通过 Contents API 上传小型握手文件，并对瞬时错误重试。"""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".signal",
    ) as signal_file:
        signal_file.write(f"{time.time():.6f}\n")
        signal_file.flush()
        for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
            try:
                contents_client.upload(signal_file.name, remote_path)
                return
            except Exception:
                if attempt >= DOWNLOAD_MAX_ATTEMPTS:
                    raise
                time.sleep(constants.DOWNLOAD_RETRY_DELAY_SECONDS)


def contents_download_with_retries(
    contents_client,
    remote_path: str,
    local_path: Path,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    activity_callback=None,
    log=print,
) -> None:
    """在存活的执行期间通过 ContentsClient 下载并校验一个文件。"""
    temporary_path = local_path.with_name(f".{local_path.name}.download")
    local_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        temporary_path.unlink(missing_ok=True)
        if activity_callback is not None:
            activity_callback()
        try:
            contents_client.download(remote_path, str(temporary_path))
        except Exception as error:
            download_error = error
        else:
            valid_download = temporary_path.is_file()
            if valid_download and expected_size is not None:
                valid_download = temporary_path.stat().st_size == expected_size
            if valid_download and expected_sha256 is not None:
                valid_download = (
                    file_sha256(temporary_path) == expected_sha256.lower()
                )
            if valid_download:
                temporary_path.replace(local_path)
                return
            download_error = RuntimeError(
                f"文件大小或 SHA-256 校验不符: {remote_path}"
            )

        temporary_path.unlink(missing_ok=True)
        if attempt < DOWNLOAD_MAX_ATTEMPTS:
            log(
                f">>> Contents API 下载失败，"
                f"{constants.DOWNLOAD_RETRY_DELAY_SECONDS} 秒后重试 "
                f"({attempt + 1}/{DOWNLOAD_MAX_ATTEMPTS}): {remote_path}"
            )
            time.sleep(constants.DOWNLOAD_RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Contents API 下载失败（已尝试 {DOWNLOAD_MAX_ATTEMPTS} 次）: "
        f"{remote_path}"
    ) from download_error


def download_with_retries(
    command_builder,
    remote_path: str,
    local_path: Path,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    log=print,
) -> None:
    """从 Colab 下载单个文件，失败或校验不符时自动重试。

    `command_builder(remote_path, local_path)` 返回完整命令列表，
    由调用方注入 `--config` 等参数（见 cli.ColabCli）。
    """
    temporary_path = local_path.with_name(f".{local_path.name}.download")
    local_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        temporary_path.unlink(missing_ok=True)
        command = command_builder(remote_path, temporary_path)
        log("+ " + " ".join(command))
        result = subprocess.run(command, check=False)
        valid_download = result.returncode == 0 and temporary_path.is_file()
        if valid_download and expected_size is not None:
            valid_download = temporary_path.stat().st_size == expected_size
        if valid_download and expected_sha256 is not None:
            valid_download = file_sha256(temporary_path) == expected_sha256
        if valid_download:
            temporary_path.replace(local_path)
            return

        temporary_path.unlink(missing_ok=True)
        if attempt < DOWNLOAD_MAX_ATTEMPTS:
            log(
                f">>> 下载或校验失败，{constants.DOWNLOAD_RETRY_DELAY_SECONDS} "
                f"秒后重试 ({attempt + 1}/{DOWNLOAD_MAX_ATTEMPTS})..."
            )
            time.sleep(constants.DOWNLOAD_RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"下载失败或文件校验不符（已尝试 {DOWNLOAD_MAX_ATTEMPTS} 次）: "
        f"{remote_path}"
    )
