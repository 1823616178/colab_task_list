"""CliTransport：全部通过 `colab` 子进程完成，不依赖任何私有 API。

降级路径（doc/01 §5 R1）：即使 google-colab-cli 重构内部 API，
本通道依然可用，只是少了「VM 存活期内实时下载」的性能优化。

CLI 状态文件隔离（doc/05 P0.2 / doc/06 §3.2）：
- 设置 `COLAB_CLI_STATE_DIR` → 每 session 一个状态文件
  `<dir>/<session>.json`，避免并发任务竞争默认状态文件的
  会话名→endpoint 映射；
- 否则设置 `COLAB_CONFIG` → 全部会话共用指定文件（开发隔离）；
- 都不设置 → 不传 `--config`，沿用 CLI 默认状态文件（旧行为）。
"""
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from .base import EventSink, Transport, TransportError, _null_sink
from .constants import (
    COLAB_EXEC_MAX_ATTEMPTS,
    COLAB_OUTPUT_TAIL_CHARS,
    REMOTE_OUTPUT_ARCHIVE,
    REMOTE_TRANSFER_DIR,
)
from .native import OutputStreamReceiver
from .transfer import (
    assemble_output_archive,
    download_with_retries,
    load_transfer_manifest,
)

_SESSION_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_GOOGLE_DRIVE_AUTH_URL = re.compile(
    r"https://accounts\.google\.com/[^\s\x1b]+"
)
_DRIVE_MOUNT_FAILURE_MARKERS = (
    "ValueError: mount failed",
    "[colab] Error propagating:",
    "No such device or address: '/dev/tty'",
)
_PENDING_DRIVE_LOCK = threading.Lock()
_PENDING_DRIVE_PROCESSES: dict[str, subprocess.Popen] = {}


def confirm_drive_mount(session: str) -> bool:
    """向正在等待人工授权的 ``colab drivemount`` 伪终端发送 Enter。"""
    with _PENDING_DRIVE_LOCK:
        process = _PENDING_DRIVE_PROCESSES.get(session)
        if process is None or process.poll() is not None or process.stdin is None:
            return False
        try:
            process.stdin.write("\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return False
        return True


def cancel_drive_mount(session: str) -> bool:
    """终止正在等待 Drive 人工授权的本地伪终端，解除任务线程阻塞。"""
    with _PENDING_DRIVE_LOCK:
        process = _PENDING_DRIVE_PROCESSES.get(session)
        if process is None or process.poll() is not None:
            return False
        setattr(process, "_colab_drive_canceled", True)
        try:
            # run_interactive_streaming 用 start_new_session 建独立进程组；同时终止
            # script 与其 colab 子进程，不能只杀外层留下孤儿授权进程。
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate()
            except (ProcessLookupError, OSError):
                return False
        return True


def _register_drive_process(session: str, process: subprocess.Popen) -> None:
    with _PENDING_DRIVE_LOCK:
        _PENDING_DRIVE_PROCESSES[session] = process


def _unregister_drive_process(session: str, process: subprocess.Popen) -> None:
    with _PENDING_DRIVE_LOCK:
        if _PENDING_DRIVE_PROCESSES.get(session) is process:
            del _PENDING_DRIVE_PROCESSES[session]


def resolve_config_path(session: str | None = None) -> Path | None:
    """按环境变量解析本次调用应使用的 CLI 状态文件。"""
    state_dir = os.environ.get("COLAB_CLI_STATE_DIR")
    if state_dir:
        directory = Path(state_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        name = _SESSION_SAFE.sub("_", session or "default")
        return directory / f"{name}.json"
    single = os.environ.get("COLAB_CONFIG")
    if single:
        return Path(single).expanduser().resolve()
    return None


def is_pre_execution_connection_loss(output: str) -> bool:
    """判断 Colab CLI 是否在提交用户脚本前丢失了内核连接。"""
    return (
        "Connection was lost." in output
        and "os.makedirs('/content'" in output
        and "os.chdir('/content')" in output
    )


class ColabCli:
    """`colab` 子进程运行器：统一注入 --config 并回显命令。"""

    def __init__(self, session: str | None = None, log=print) -> None:
        self._config_path = resolve_config_path(session)
        self._log = log

    def build(self, *args) -> list[str]:
        command = ["colab"]
        if self._config_path is not None:
            command += ["--config", str(self._config_path)]
        command += [str(arg) for arg in args]
        return command

    def run(self, *args, check=True, echo=None) -> subprocess.CompletedProcess:
        """运行命令并回显；check 时非零退出抛 TransportError（旧 sys.exit 文案）。"""
        command = self.build(*args)
        self._log(echo if echo is not None else "+ " + " ".join(command))
        result = subprocess.run(command)
        if check and result.returncode != 0:
            raise TransportError(
                f"colab {args[0]} 失败 (exit {result.returncode})"
            )
        return result

    def run_capture(self, *args, echo=None) -> subprocess.CompletedProcess:
        """运行命令并捕获输出（stdout/stderr 不合并）。"""
        command = self.build(*args)
        if echo is not False:
            self._log(echo if isinstance(echo, str) else "+ " + " ".join(command))
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def run_streaming(self, command: list[str], on_line) -> tuple[int, str]:
        """运行命令、逐行回调合并输出，并保留有限长度的输出尾部。"""
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_tail = ""
        if process.stdout is not None:
            for output_line in process.stdout:
                on_line(output_line)
                output_tail = (output_tail + output_line)[-COLAB_OUTPUT_TAIL_CHARS:]
        return process.wait(), output_tail

    def run_interactive_streaming(
        self,
        command: list[str],
        on_line,
        on_started=None,
        on_finished=None,
    ) -> tuple[int, str]:
        """在伪终端中运行交互命令，同时逐行转发输出。

        ``colab drivemount`` 会显式打开 ``/dev/tty`` 等待人工按 Enter；Runner
        是无终端后台服务，因此用 util-linux ``script`` 为子进程提供控制终端。
        stdin 保留为 PIPE，授权完成后可由 HTTP 确认接口写入换行。
        """
        script_bin = shutil.which("script")
        if script_bin is None:
            return self.run_streaming(command, on_line)
        wrapped = [script_bin, "-qefc", shlex.join(command), os.devnull]
        process = subprocess.Popen(
            wrapped,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        output_tail = ""
        try:
            if on_started is not None:
                on_started(process)
            if process.stdout is not None:
                for output_line in process.stdout:
                    on_line(output_line)
                    output_tail = (
                        output_tail + output_line
                    )[-COLAB_OUTPUT_TAIL_CHARS:]
            return process.wait(), output_tail
        finally:
            if on_finished is not None:
                on_finished(process)
            if process.stdin is not None:
                process.stdin.close()


class CliTransport(Transport):
    """纯 subprocess 通道。"""

    mode = "cli"

    def __init__(self, sink: EventSink | None = None) -> None:
        self._sink = sink if sink is not None else _null_sink

    def _log(self, text: str, stream: str = "stdout") -> None:
        self._sink(
            "log.line", "progress", text, {"stream": stream, "text": text}
        )

    def _cli(self, session: str) -> ColabCli:
        return ColabCli(session, log=self._log)

    def create_session(self, session: str, kind: str, spec: str) -> None:
        cli = self._cli(session)
        if kind == "tpu":
            cli.run("new", "-s", session, "--tpu", spec)
        else:
            cli.run("new", "-s", session, "--gpu", spec)

    def mount_drive(self, session: str) -> bool:
        cli = self._cli(session)
        command = cli.build("drivemount", "-s", session)
        self._log("+ " + " ".join(command))
        started_processes: list[subprocess.Popen] = []

        def on_line(output_line: str) -> None:
            text = output_line.rstrip("\r\n")
            self._log(text)
            match = _GOOGLE_DRIVE_AUTH_URL.search(text)
            if match is not None:
                self._sink(
                    "drive.mount",
                    "start",
                    ">>> Google Drive 等待浏览器授权",
                    {
                        "authorization_url": match.group(0),
                        "confirmation_required": True,
                        "session": session,
                    },
                )

        try:
            returncode, output_tail = cli.run_interactive_streaming(
                command,
                on_line,
                on_started=lambda process: (
                    started_processes.append(process),
                    _register_drive_process(session, process),
                ),
                on_finished=lambda process: _unregister_drive_process(
                    session, process
                ),
            )
        except OSError as error:
            self._log(f">>> Google Drive 挂载命令启动失败: {error}", "stderr")
            return False
        canceled = bool(
            started_processes
            and getattr(
                started_processes[-1], "_colab_drive_canceled", False
            )
        )
        reported_failure = any(
            marker in output_tail for marker in _DRIVE_MOUNT_FAILURE_MARKERS
        )
        if returncode != 0 or reported_failure or canceled:
            detail = (
                "已收到取消请求"
                if canceled
                else f"exit {returncode}"
                if returncode != 0
                else "CLI 已报告 mount failed"
            )
            self._log(
                f">>> Google Drive 挂载失败 ({detail})，"
                "将使用 VM 本地模型缓存",
                "stderr",
            )
            return False
        return True

    def stop_session(self, session: str) -> None:
        self._cli(session).run("stop", "-s", session, check=False)

    def session_status(self, session: str) -> str:
        """返回 colab status 的合并文本；回显命令（旧行为一致）。"""
        result = self._cli(session).run_capture("status", "-s", session)
        return "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part.strip()
        )

    def check_session_readable(self, session: str,
                               sink: EventSink = _null_sink) -> None:
        cli = self._cli(session)
        command = cli.build("ls", "-s", session, "/content")
        echo = f"+ {' '.join(command)} (可用性检查)"
        self._log(echo)
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            if details:
                self._log(details, stream="stderr")
            raise TransportError(
                f"无法复用会话 {session!r}；请确认会话名称，"
                "或在 start.sh 中选择新建 VM"
            )

    def upload(self, session: str, local_path, remote_path: str) -> None:
        self._cli(session).run("upload", "-s", session, str(local_path), remote_path)

    def exec_script(
        self,
        session: str,
        script_path: Path,
        timeout: float,
        sink: EventSink = _null_sink,
    ) -> None:
        """执行远程脚本，并安全重试发生在脚本提交前的连接中断。"""

        cli = self._cli(session)
        command = cli.build(
            "exec", "-s", session, "-f", str(script_path),
            "--timeout", str(timeout),
        )
        for attempt in range(1, COLAB_EXEC_MAX_ATTEMPTS + 1):
            self._log("+ " + " ".join(command))
            returncode, output_tail = cli.run_streaming(
                command,
                lambda line: self._log(line.rstrip("\n")),
            )
            if returncode == 0:
                return

            retryable = is_pre_execution_connection_loss(output_tail)
            if not retryable or attempt >= COLAB_EXEC_MAX_ATTEMPTS:
                raise TransportError(
                    f"colab exec 失败 (exit {returncode}, "
                    f"尝试 {attempt}/{COLAB_EXEC_MAX_ATTEMPTS})"
                )

            self._log(
                f">>> Colab 内核连接在提交脚本前断开，"
                f"正在重新连接并重试 ({attempt + 1}/"
                f"{COLAB_EXEC_MAX_ATTEMPTS})..."
            )
            status_result = subprocess.run(
                cli.build("status", "-s", session),
                check=False,
            )
            if status_result.returncode != 0:
                raise TransportError(
                    f"会话 {session!r} 已不可访问，无法自动重试"
                )
            time.sleep(2)

    def exec_workflow(
        self,
        session: str,
        script_path: Path,
        timeout: float,
        output_zip: Path,
        sink: EventSink = _null_sink,
    ) -> bool:
        """经执行通道流接收输出（Contents API 不可用时的兜底通道）。

        远端在初始等待超时后会改用 stdout 流式传输输出分块，
        本地由 OutputStreamReceiver 重组校验；未流式取回时返回 False，
        由调用方走 download_output。
        """

        effective_sink = sink if sink is not _null_sink else self._sink
        cli = self._cli(session)
        receiver = OutputStreamReceiver(
            output_zip, contents_client=None, sink=effective_sink
        )
        command = cli.build(
            "exec", "-s", session, "-f", str(script_path),
            "--timeout", str(timeout),
        )
        for attempt in range(1, COLAB_EXEC_MAX_ATTEMPTS + 1):
            self._log("+ " + " ".join(command))
            returncode, output_tail = cli.run_streaming(command, receiver.feed_stdout)
            if returncode == 0:
                return receiver.finalize()

            retryable = is_pre_execution_connection_loss(output_tail)
            if not retryable or attempt >= COLAB_EXEC_MAX_ATTEMPTS:
                raise TransportError(
                    f"colab exec 失败 (exit {returncode}, "
                    f"尝试 {attempt}/{COLAB_EXEC_MAX_ATTEMPTS})"
                )

            self._log(
                f">>> Colab 内核连接在提交脚本前断开，"
                f"正在重新连接并重试 ({attempt + 1}/"
                f"{COLAB_EXEC_MAX_ATTEMPTS})..."
            )
            status_result = subprocess.run(
                cli.build("status", "-s", session),
                check=False,
            )
            if status_result.returncode != 0:
                raise TransportError(
                    f"会话 {session!r} 已不可访问，无法自动重试"
                )
            time.sleep(2)
        return False

    def remote_output_ready(self, session: str) -> bool | None:
        """单次尝试下载远端 manifest.json：成功 = 远端已写完全部分块。"""
        import tempfile

        cli = self._cli(session)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "manifest.json"
            result = subprocess.run(
                cli.build("download", "-s", session,
                          f"{REMOTE_TRANSFER_DIR}/manifest.json", str(target)),
                capture_output=True, text=True, timeout=120, check=False,
            )
            return result.returncode == 0 and target.is_file()

    def download_output(self, session: str, output_zip: Path,
                        sink: EventSink = _null_sink) -> None:
        """下载远端输出分块，重组并验证最终 ZIP。"""

        effective_sink = sink if sink is not _null_sink else self._sink
        cli = self._cli(session)

        def build_download(remote_path: str, local_path: Path) -> list[str]:
            return cli.build("download", "-s", session, remote_path, str(local_path))

        transfer_root = output_zip.parent / ".output_transfer"
        manifest_path = transfer_root / "manifest.json"
        parts_dir = transfer_root / "parts"
        shutil.rmtree(transfer_root, ignore_errors=True)
        parts_dir.mkdir(parents=True)

        try:
            try:
                download_with_retries(
                    build_download,
                    f"{REMOTE_TRANSFER_DIR}/manifest.json",
                    manifest_path,
                    log=self._log,
                )
            except RuntimeError:
                self._log(">>> 未找到分块清单，尝试兼容旧版本并直接下载 output.zip...")
                download_with_retries(
                    build_download,
                    REMOTE_OUTPUT_ARCHIVE,
                    output_zip,
                    log=self._log,
                )
                with zipfile.ZipFile(output_zip) as archive:
                    corrupt_member = archive.testzip()
                if corrupt_member is not None:
                    output_zip.unlink(missing_ok=True)
                    raise RuntimeError(f"输出 ZIP 中的文件已损坏: {corrupt_member}")
                return

            manifest = load_transfer_manifest(manifest_path)
            parts = manifest["parts"]
            effective_sink(
                "output.transfer",
                "start",
                f">>> 开始取回输出: "
                f"{manifest['archive_size'] / 1024**2:.1f} MiB，"
                f"共 {len(parts)} 个分块",
                {
                    "archive_size": manifest["archive_size"],
                    "archive_sha256": manifest["archive_sha256"],
                    "parts": len(parts),
                },
            )

            for index, part in enumerate(parts, start=1):
                effective_sink(
                    "output.transfer",
                    "progress",
                    f">>> 下载分块 {index}/{len(parts)}: {part['name']}",
                    {
                        "part": index,
                        "parts": len(parts),
                        "part_name": part["name"],
                        "transport": self.mode,
                    },
                )
                download_with_retries(
                    build_download,
                    f"{REMOTE_TRANSFER_DIR}/{part['name']}",
                    parts_dir / part["name"],
                    expected_size=part["size"],
                    expected_sha256=part["sha256"],
                    log=self._log,
                )

            assemble_output_archive(manifest, parts_dir, output_zip)
        finally:
            shutil.rmtree(transfer_root, ignore_errors=True)
