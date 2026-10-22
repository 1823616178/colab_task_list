"""NativeTransport：内核执行通道 + Contents API 实时下载（性能优化路径）。

风险登记（doc/01 §5 R1）：本模块是全项目私有 API 集中度最高的地方
（ContentsClient._request 覆写、ColabRuntime.kernel_client）。
启动时必须经 `_probe_private_api()` 自检，失败降级 CliTransport。

`google-colab-cli` 的内部导入全部延迟到方法体内，模块本身可安全 import。
"""
import base64
import hashlib
import json
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .base import EventSink, Transport, _null_sink
from .constants import (
    COLAB_EXEC_MAX_ATTEMPTS,
    OUTPUT_STREAM_PREFIX,
    REMOTE_TRANSFER_ACK,
    REMOTE_TRANSFER_ACTIVE,
    REMOTE_TRANSFER_DIR,
    REMOTE_TRANSFER_STREAM_REQUEST,
    STREAM_PAYLOAD_METRICS,
    STREAM_PAYLOAD_PROGRESS,
)
from .transfer import (
    assemble_output_archive,
    contents_download_with_retries,
    load_transfer_manifest,
    upload_contents_signal,
    valid_sha256,
)


def download_live_output_archive(
    contents_client,
    output_zip: Path,
    ready_metadata: dict,
    sink: EventSink = _null_sink,
) -> None:
    """在远端执行仍存活时下载清单和分块，并持续刷新活跃标记。"""
    transfer_root = output_zip.parent / ".output_transfer_live"
    manifest_path = transfer_root / "manifest.json"
    parts_dir = transfer_root / "parts"
    shutil.rmtree(transfer_root, ignore_errors=True)
    parts_dir.mkdir(parents=True)

    def log(text: str) -> None:
        sink("output.transfer", "progress", text, {"transport": "native"})

    def signal_activity() -> None:
        upload_contents_signal(contents_client, REMOTE_TRANSFER_ACTIVE)

    try:
        contents_download_with_retries(
            contents_client,
            f"{REMOTE_TRANSFER_DIR}/manifest.json",
            manifest_path,
            activity_callback=signal_activity,
            log=log,
        )
        manifest = load_transfer_manifest(manifest_path)
        if manifest["archive_size"] != ready_metadata["size"]:
            raise RuntimeError("READY 元数据和分块清单的归档大小不一致")
        if manifest["archive_sha256"].lower() != ready_metadata["sha256"].lower():
            raise RuntimeError("READY 元数据和分块清单的 SHA-256 不一致")
        if len(manifest["parts"]) != ready_metadata["parts"]:
            raise RuntimeError("READY 元数据和分块清单的分块数不一致")

        for index, part in enumerate(manifest["parts"], start=1):
            sink(
                "output.transfer",
                "progress",
                f">>> 下载输出分块 {index}/{len(manifest['parts'])}: "
                f"{part['name']}",
                {
                    "part": index,
                    "parts": len(manifest["parts"]),
                    "part_name": part["name"],
                    "transport": "native",
                },
            )
            contents_download_with_retries(
                contents_client,
                f"{REMOTE_TRANSFER_DIR}/{part['name']}",
                parts_dir / part["name"],
                expected_size=part["size"],
                expected_sha256=part["sha256"],
                activity_callback=signal_activity,
                log=log,
            )
        assemble_output_archive(manifest, parts_dir, output_zip)
    finally:
        shutil.rmtree(transfer_root, ignore_errors=True)


class OutputStreamReceiver:
    """优先通过 Contents API、必要时通过 stdout 接收输出 ZIP。

    `sink(phase, status, message, data)` 接收三类本地事件：
    - 非协议行 → log.line（CLI 原样打印）；
    - `__COLAB_OUTPUT_V1__PROGRESS:{json}` → workflow.exec progress；
    - `__COLAB_OUTPUT_V1__METRICS:{json}` → metrics.sample。
    """

    def __init__(self, output_path: Path, contents_client=None,
                 sink: EventSink = _null_sink) -> None:
        """初始化流解析状态和原子写入临时文件。"""
        self.output_path = output_path
        self.contents_client = contents_client
        self.sink = sink
        self.temporary_path = output_path.with_name(
            f".{output_path.name}.streaming"
        )
        self.completed = False
        self._live_download_error = None
        self._buffer = ""
        self._destination = None
        self._discard_stream = False
        self._digest = hashlib.sha256()
        self._expected_chunk = 0
        self._expected_size = None
        self._expected_sha256 = None
        self._received_size = 0

    def handle_output(self, output: dict) -> None:
        """处理 ColabRuntime 发出的单个 Jupyter 输出对象。"""
        output_type = output.get("output_type")
        if output_type == "stream":
            text = output.get("text", "")
            if output.get("name") == "stderr":
                text = text.rstrip("\n")
                if text:
                    self.sink(
                        "log.line",
                        "progress",
                        text,
                        {"stream": "stderr", "text": text},
                    )
            else:
                self.feed_stdout(text)
            return
        if output_type == "error":
            traceback_lines = output.get("traceback", [])
            if traceback_lines:
                text = "".join(traceback_lines)
            else:
                text = (
                    f"{output.get('ename', 'Error')}: "
                    f"{output.get('evalue', '')}"
                )
            self.sink(
                "log.line",
                "progress",
                text,
                {"stream": "stderr", "text": text},
            )

    def feed_stdout(self, text: str) -> None:
        """按行解析 stdout；普通日志继续实时显示。"""
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._process_line(line)

    def finalize(self) -> bool:
        """结束解析并返回是否收到完整的内嵌 ZIP。"""
        if self._buffer:
            self._process_line(self._buffer)
            self._buffer = ""
        if self._destination is not None:
            self._destination.close()
            self._destination = None
            self.temporary_path.unlink(missing_ok=True)
            raise RuntimeError("远端输出流在 END 标记前中断")
        if not self.completed and self._live_download_error is not None:
            raise RuntimeError(
                "Contents API 下载失败，且未收到完整的执行通道兜底输出"
            ) from self._live_download_error
        return self.completed

    def abort(self) -> None:
        """关闭未完成的临时输出并删除残留文件。"""
        if self._destination is not None:
            self._destination.close()
            self._destination = None
        self.temporary_path.unlink(missing_ok=True)

    def _log(self, text: str) -> None:
        self.sink("log.line", "progress", text, {"stream": "stdout", "text": text})

    def _process_line(self, line: str) -> None:
        """解析协议行或转发普通日志行。"""
        if not line.startswith(OUTPUT_STREAM_PREFIX):
            self._log(line)
            return

        payload = line[len(OUTPUT_STREAM_PREFIX):]
        if payload.startswith("READY:"):
            self._handle_ready(payload[len("READY:"):])
        elif payload.startswith("BEGIN:"):
            self._begin(payload[len("BEGIN:"):])
        elif payload.startswith("CHUNK:"):
            self._write_chunk(payload[len("CHUNK:"):])
        elif payload.startswith("END:"):
            self._finish(payload[len("END:"):])
        elif payload.startswith(STREAM_PAYLOAD_PROGRESS):
            self._handle_structured(
                payload[len(STREAM_PAYLOAD_PROGRESS):], "workflow.exec"
            )
        elif payload.startswith(STREAM_PAYLOAD_METRICS):
            self._handle_structured(
                payload[len(STREAM_PAYLOAD_METRICS):], "metrics.sample"
            )
        else:
            raise RuntimeError("收到未知的远端输出流协议行")

    def _handle_structured(self, payload_text: str, phase: str) -> None:
        """把远端的结构化进度/资源行转成事件；坏行降级为普通日志。"""
        try:
            data = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError):
            self._log(payload_text)
            return
        if not isinstance(data, dict):
            self._log(payload_text)
            return
        text = data.get("text", "")
        self.sink(phase, "progress", text, data)

    def _handle_ready(self, metadata_text: str) -> None:
        """收到远端就绪信号后，在 VM 存活期间立即下载输出分块。"""
        metadata = self._parse_metadata(metadata_text, require_parts=True)
        if self.completed:
            return
        if self.contents_client is None:
            self._log(">>> 当前执行端没有 Contents API 客户端，等待 stdout 兜底")
            return

        self.sink(
            "output.transfer",
            "start",
            f">>> 输出已就绪，开始在远端任务结束前下载: "
            f"{metadata['size'] / 1024**2:.1f} MiB，"
            f"{metadata['parts']} 个分块",
            {
                "archive_size": metadata["size"],
                "archive_sha256": metadata["sha256"],
                "parts": metadata["parts"],
                "transport": "native",
            },
        )
        try:
            upload_contents_signal(
                self.contents_client,
                REMOTE_TRANSFER_ACTIVE,
            )
            download_live_output_archive(
                self.contents_client,
                self.output_path,
                metadata,
                sink=self.sink,
            )
        except Exception as error:
            self._live_download_error = error
            self.sink(
                "output.transfer",
                "warn",
                f">>> Contents API 实时下载失败: {error}；"
                "请求 stdout 兜底传输",
                {"reason": "contents_live_download_failed", "fallback": "stdout"},
            )
            self._request_stream_fallback()
            return

        self.completed = True
        self.sink(
            "output.transfer",
            "progress",
            ">>> 输出分块下载及完整性校验完成",
            {
                "local_path": str(self.output_path),
                "size_bytes": metadata["size"],
                "sha256": metadata["sha256"],
                "verified": True,
                "transport": "native",
            },
        )
        try:
            upload_contents_signal(
                self.contents_client,
                REMOTE_TRANSFER_ACK,
            )
        except Exception as error:
            self._log(
                f">>> 下载已完成，但远端确认上传失败: {error}；"
                "可能收到一份可忽略的 stdout 兜底流"
            )
            self._request_stream_fallback()

    def _request_stream_fallback(self) -> None:
        """尽力通知远端改用 stdout 传输；通知失败时由远端超时兜底。"""
        if self.contents_client is None:
            return
        try:
            upload_contents_signal(
                self.contents_client,
                REMOTE_TRANSFER_STREAM_REQUEST,
            )
        except Exception as error:
            self._log(f">>> 无法发送 stdout 兜底请求: {error}；等待远端自动回退")

    @staticmethod
    def _parse_metadata(metadata_text: str, require_parts: bool = False) -> dict:
        """解析并验证 READY/BEGIN 共用的归档元数据。"""
        try:
            metadata = json.loads(metadata_text)
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError("远端输出元数据不是有效 JSON") from error
        expected_size = metadata.get("size")
        expected_sha256 = metadata.get("sha256")
        if not isinstance(expected_size, int) or expected_size < 0:
            raise RuntimeError("远端输出流的文件大小无效")
        if not valid_sha256(expected_sha256):
            raise RuntimeError("远端输出流的 SHA-256 无效")
        if require_parts:
            parts = metadata.get("parts")
            if not isinstance(parts, int) or parts <= 0:
                raise RuntimeError("远端输出分块数量无效")
        return metadata

    def _begin(self, metadata_text: str) -> None:
        """验证 BEGIN 元数据并创建临时输出。"""
        if self._destination is not None:
            raise RuntimeError("收到重复的远端输出 BEGIN 标记")
        metadata = self._parse_metadata(metadata_text)
        expected_size = metadata["size"]
        expected_sha256 = metadata["sha256"]

        self._expected_chunk = 0
        if self.completed:
            self._discard_stream = True
            self._log(">>> 本地输出已完整，忽略重复的 stdout 兜底流")
            return

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.temporary_path.unlink(missing_ok=True)
        self._discard_stream = False
        self._digest = hashlib.sha256()
        self._received_size = 0
        self._expected_size = expected_size
        self._expected_sha256 = expected_sha256
        self._destination = self.temporary_path.open("wb")
        self.sink(
            "output.transfer",
            "progress",
            f">>> 正在通过执行通道取回输出: "
            f"{expected_size / 1024**2:.1f} MiB",
            {"transport": "stdout"},
        )

    def _write_chunk(self, chunk_payload: str) -> None:
        """解码并顺序写入一个 Base64 数据块。"""
        if self._destination is None and not self._discard_stream:
            raise RuntimeError("在 BEGIN 标记前收到输出数据块")
        try:
            chunk_index_text, encoded_chunk = chunk_payload.split(":", 1)
            chunk_index = int(chunk_index_text)
        except (ValueError, TypeError) as error:
            raise RuntimeError("远端输出数据块格式无效") from error
        if chunk_index != self._expected_chunk:
            raise RuntimeError(
                f"远端输出数据块顺序错误: 期望 {self._expected_chunk}，"
                f"收到 {chunk_index}"
            )
        if self._discard_stream:
            self._expected_chunk += 1
            return
        try:
            chunk = base64.b64decode(encoded_chunk, validate=True)
        except (ValueError, TypeError) as error:
            raise RuntimeError("远端输出数据块 Base64 无效") from error
        self._destination.write(chunk)
        self._digest.update(chunk)
        self._received_size += len(chunk)
        self._expected_chunk += 1

    def _finish(self, chunk_count_text: str) -> None:
        """处理 END 标记并验证、提交最终 ZIP。"""
        if self._destination is None and not self._discard_stream:
            raise RuntimeError("在 BEGIN 标记前收到 END 标记")
        try:
            chunk_count = int(chunk_count_text)
        except ValueError as error:
            raise RuntimeError("远端输出 END 标记无效") from error
        if chunk_count != self._expected_chunk:
            raise RuntimeError(
                f"远端输出数据块数量错误: 期望 {self._expected_chunk}，"
                f"收到 {chunk_count}"
            )
        if self._discard_stream:
            self._discard_stream = False
            return

        self._destination.flush()
        os.fsync(self._destination.fileno())
        self._destination.close()
        self._destination = None
        if self._received_size != self._expected_size:
            self.temporary_path.unlink(missing_ok=True)
            raise RuntimeError("远端输出流大小校验失败")
        if self._digest.hexdigest() != self._expected_sha256.lower():
            self.temporary_path.unlink(missing_ok=True)
            raise RuntimeError("远端输出流 SHA-256 校验失败")
        with zipfile.ZipFile(self.temporary_path) as archive:
            corrupt_member = archive.testzip()
        if corrupt_member is not None:
            self.temporary_path.unlink(missing_ok=True)
            raise RuntimeError(f"远端输出 ZIP 中的文件已损坏: {corrupt_member}")
        self.temporary_path.replace(self.output_path)
        self.completed = True
        self.sink(
            "output.transfer",
            "progress",
            ">>> 执行通道输出接收及完整性校验完成",
            {
                "local_path": str(self.output_path),
                "size_bytes": self._expected_size,
                "sha256": self._expected_sha256,
                "verified": True,
                "transport": "stdout",
            },
        )


def execute_runtime_streaming(runtime, code: str, receiver, timeout_value):
    """执行代码并逐条处理 IOPub 消息，避免把完整 stdout 留在内存。"""
    errors = []

    def handle_message(message: dict) -> None:
        message_type = message.get("header", {}).get("msg_type")
        content = message.get("content", {})
        if message_type == "stream":
            receiver.handle_output({
                "output_type": "stream",
                "name": content.get("name"),
                "text": content.get("text", ""),
            })
        elif message_type == "error":
            output = {
                "output_type": "error",
                "ename": content.get("ename", "Error"),
                "evalue": content.get("evalue", ""),
                "traceback": content.get("traceback", []),
            }
            errors.append(output)
            receiver.handle_output(output)

    reply = runtime.kernel_client.execute_interactive(
        code,
        output_hook=handle_message,
        timeout=timeout_value,
    )
    reply_content = reply.get("content", {}) if reply else {}
    if reply_content.get("status") == "error" and not errors:
        errors.append({
            "output_type": "error",
            "ename": reply_content.get("ename", "Error"),
            "evalue": reply_content.get("evalue", "Unknown error"),
            "traceback": reply_content.get("traceback", []),
        })
    return errors


class NativeTransport(Transport):
    """内核通道：工作流执行期间即可经 Contents API 实时取回输出。

    会话创建/上传/状态等仍走公共 CLI（与子进程共享 --config 隔离）。
    """

    mode = "native"

    def __init__(self, sink: EventSink | None = None) -> None:
        from .cli import CliTransport

        self._sink = sink if sink is not None else _null_sink
        self._cli = CliTransport(self._sink)

    def create_session(self, session: str, kind: str, spec: str) -> None:
        self._cli.create_session(session, kind, spec)

    def mount_drive(self, session: str) -> bool:
        return self._cli.mount_drive(session)

    def stop_session(self, session: str) -> None:
        self._cli.stop_session(session)

    def session_status(self, session: str) -> str:
        return self._cli.session_status(session)

    def check_session_readable(self, session: str,
                               sink: EventSink = _null_sink) -> None:
        self._cli.check_session_readable(session, sink)

    def upload(self, session: str, local_path, remote_path: str) -> None:
        self._cli.upload(session, local_path, remote_path)

    def exec_script(
        self,
        session: str,
        script_path: Path,
        timeout: float,
        sink: EventSink = _null_sink,
    ) -> None:
        self._cli.exec_script(session, script_path, timeout, sink)

    def download_output(self, session: str, output_zip: Path,
                        sink: EventSink = _null_sink) -> None:
        self._cli.download_output(session, output_zip, sink)

    def remote_output_ready(self, session: str) -> bool | None:
        return self._cli.remote_output_ready(session)

    def exec_workflow(
        self,
        session: str,
        script_path: Path,
        timeout: float,
        output_zip: Path,
        sink: EventSink = _null_sink,
    ) -> bool:
        """执行远端工作流，并在任务退出前完成结果交接。"""
        effective_sink = sink if sink is not _null_sink else self._sink
        from colab_cli.common import state
        from colab_cli.contents import ContentsClient
        from colab_cli.runtime import ColabRuntime
        from colab_cli.utils import get_status_code
        import requests
        from urllib.parse import quote

        class TimedContentsClient(ContentsClient):
            """为上游未设置超时的 Contents API 请求补充连接/读取上限。"""

            def _request(self, method, path, params=None, json_data=None):
                quoted_path = quote(path.strip("/"), safe="/")
                url = f"{self.base_url}/api/contents/{quoted_path}"
                request_params = {
                    "authuser": "0",
                    "colab-runtime-proxy-token": self.token,
                }
                if params:
                    request_params.update(params)
                response = requests.request(
                    method,
                    url,
                    params=request_params,
                    json=json_data,
                    timeout=(10, 120),
                )
                if get_status_code(response) == 404:
                    raise FileNotFoundError(
                        f"File or directory not found: {path}"
                    )
                response.raise_for_status()
                if method == "DELETE":
                    return None
                return response.json()

        session_state = state.store.get(session)
        if session_state is None:
            raise RuntimeError(f"本地没有会话映射: {session}")

        def on_kernel_started(kernel_id: str) -> None:
            session_state.kernel_id = kernel_id
            state.store.add(session_state)

        def on_session_started(session_id: str) -> None:
            session_state.session_id = session_id
            state.store.add(session_state)

        def create_runtime() -> ColabRuntime:
            return ColabRuntime(
                session_state.url,
                session_state.token,
                kernel_id=session_state.kernel_id,
                session_id=session_state.session_id,
                on_kernel_started=on_kernel_started,
                on_session_started=on_session_started,
            )

        def log(text: str, stream: str = "stdout") -> None:
            effective_sink(
                "log.line", "progress", text, {"stream": stream, "text": text}
            )

        runtime = None
        preflight_code = (
            "import os; os.makedirs('/content', exist_ok=True); "
            "os.chdir('/content')"
        )
        for attempt in range(1, COLAB_EXEC_MAX_ATTEMPTS + 1):
            runtime = create_runtime()
            try:
                preflight_outputs = runtime.execute_code(preflight_code, timeout=60)
                preflight_errors = [
                    output for output in preflight_outputs
                    if output.get("output_type") == "error"
                ]
                if preflight_errors:
                    error = preflight_errors[-1]
                    raise RuntimeError(
                        f"远端预检失败: {error.get('ename', 'Error')}: "
                        f"{error.get('evalue', '')}"
                    )
                break
            except Exception as error:
                runtime.stop()
                runtime = None
                if attempt >= COLAB_EXEC_MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"连接 Colab 内核失败（已尝试 "
                        f"{COLAB_EXEC_MAX_ATTEMPTS} 次）"
                    ) from error
                log(
                    f">>> 工作流尚未提交，Colab 内核预检失败；"
                    f"正在建立新内核连接 "
                    f"({attempt + 1}/{COLAB_EXEC_MAX_ATTEMPTS})..."
                )
                session_state.kernel_id = None
                session_state.session_id = None
                state.store.add(session_state)
                time.sleep(2)

        receiver = OutputStreamReceiver(
            output_zip,
            contents_client=TimedContentsClient(session_state),
            sink=effective_sink,
        )
        timeout_value = None if timeout <= 0 else timeout
        try:
            session_state.running = f"exec({script_path})"
            session_state.last_execution = (
                str(script_path),
                None,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            state.store.add(session_state)
            code = script_path.read_text(encoding="utf-8")
            errors = execute_runtime_streaming(
                runtime,
                code,
                receiver,
                timeout_value,
            )
            if errors:
                error = errors[-1]
                raise RuntimeError(
                    f"远端工作流执行失败: {error.get('ename', 'Error')}: "
                    f"{error.get('evalue', '')}"
                )
            return receiver.finalize()
        except Exception as error:
            connection_ended_after_download = (
                receiver.completed
                and (
                    isinstance(error, TimeoutError)
                    or "Connection was lost" in str(error)
                )
            )
            if connection_ended_after_download:
                log(
                    ">>> 输出已经完整取回；远端连接在任务收尾阶段断开，"
                    "按成功处理"
                )
                return receiver.finalize()
            receiver.abort()
            raise
        finally:
            session_state.running = None
            state.store.add(session_state)
            if runtime is not None:
                runtime.stop()
