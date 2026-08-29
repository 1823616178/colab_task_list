"""会话级操作（自 run.py 迁入）：复用校验、按名停止、按 endpoint 停止、前台启动 VM。

文案与旧 run.py 逐字一致；sys.exit 改为 TaskError / TransportError。
"""
import re
import subprocess
import sys
import time

from colab_transport import Transport
from colab_transport.cli import ColabCli

from . import events as ev
from .errors import TaskError
from .events import EventEmitter

SESSION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")
GPU_SPECS = ("T4", "L4", "G4", "H100", "A100")
TPU_SPECS = ("v5e1", "v6e1")
CREATE_TIMEOUT_SECONDS = 180


def _command_log(command: list[str], result: subprocess.CompletedProcess) -> str:
    parts = ["+ " + " ".join(command)]
    if result.stdout:
        parts.append(result.stdout.rstrip())
    if result.stderr:
        parts.append(result.stderr.rstrip())
    return "\n".join(parts)


def create_named_session(
    name: str,
    kind: str,
    spec: str,
    mount_drive: bool = False,
) -> dict:
    """前台启动 Colab VM：`colab new`，可选 `drivemount`。返回含命令日志的摘要。"""
    if not isinstance(name, str) or not SESSION_NAME_PATTERN.match(name):
        raise ValueError("会话名仅允许字母、数字、_ . -，最长 128 字符")
    if kind not in ("gpu", "tpu"):
        raise ValueError(f"未知加速器类型: {kind}")
    valid = GPU_SPECS if kind == "gpu" else TPU_SPECS
    if spec not in valid:
        raise ValueError(f"{kind} 不支持规格 {spec}，可选: {', '.join(valid)}")

    cli = ColabCli(name)
    if kind == "tpu":
        command = cli.build("new", "-s", name, "--tpu", spec)
    else:
        command = cli.build("new", "-s", name, "--gpu", spec)
    print("+", " ".join(command), flush=True)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False,
            timeout=CREATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"创建会话超时（{CREATE_TIMEOUT_SECONDS}s）: {name}"
        ) from error
    log = _command_log(command, result)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"创建会话失败: {name}\n{log}")

    if mount_drive:
        drive_cmd = cli.build("drivemount", "-s", name)
        print("+", " ".join(drive_cmd), flush=True)
        drive = subprocess.run(
            drive_cmd, capture_output=True, text=True, check=False, timeout=120,
        )
        drive_log = _command_log(drive_cmd, drive)
        log = f"{log}\n{drive_log}"
        if drive.stdout:
            print(drive.stdout, end="", flush=True)
        if drive.stderr:
            print(drive.stderr, end="", file=sys.stderr, flush=True)
        if drive.returncode != 0:
            log += "\n(警告: Drive 挂载失败，会话已创建)"

    return {
        "ok": True,
        "name": name,
        "accelerator": {"kind": kind, "spec": spec},
        "mount_drive": mount_drive,
        "log": log,
    }


def ensure_reusable_session(
    session: str,
    transport: Transport,
    emitter: EventEmitter,
) -> str | None:
    """确认指定会话仍可访问，并返回检测到的加速器类型。"""
    status_text = transport.session_status(session)
    if status_text:
        emitter.log(status_text)
    if "Status: BUSY" in status_text:
        raise TaskError(
            f"会话 {session!r} 仍有任务运行，不能并发提交新工作流。"
            "请等待当前任务结束，或先确认并停止该会话。"
        )

    transport.check_session_readable(session)

    accelerator = None
    if "Variant: TPU" in status_text:
        accelerator = "tpu"
    elif "Variant: GPU" in status_text:
        accelerator = "gpu"
    elif "Variant: DEFAULT" in status_text:
        accelerator = "cpu"

    emitter.emit(
        ev.PHASE_SESSION_REUSE,
        ev.STATUS_OK,
        f">>> 现有会话可用: {session}",
        {"session": session, "accelerator": accelerator},
    )
    if accelerator is None:
        emitter.emit(
            ev.PHASE_SESSION_REUSE,
            ev.STATUS_WARN,
            "提示: 无法从会话状态识别加速器类型，将跳过兼容性校验",
            {"reason": "accelerator_unknown", "session": session},
        )
    return accelerator


def stop_named_session(session: str) -> str:
    """停止已记录在本地状态文件中的命名 Colab 会话。成功时返回命令日志。"""
    cli = ColabCli(session)
    command = cli.build("stop", "-s", session)
    print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    log = _command_log(command, result)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)

    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode != 0 or "session" in combined_output and "not found" in combined_output:
        raise RuntimeError(f"停止会话失败: {session}\n{log}")
    return log


def stop_unmapped_endpoint(endpoint: str) -> None:
    """通过 Runtime endpoint 停止缺少本地名称映射的云端会话。"""
    if (
        not endpoint
        or len(endpoint) > 512
        or any(character.isspace() or ord(character) < 32 for character in endpoint)
    ):
        raise ValueError(f"Runtime endpoint 格式无效: {endpoint!r}")

    from colab_cli.common import state

    assignments = state.client.list_assignments()
    active_endpoints = {assignment.endpoint for assignment in assignments}
    if endpoint not in active_endpoints:
        raise RuntimeError(f"云端 Runtime 已不存在: {endpoint}")

    print(f">>> 正在按 Runtime endpoint 停止未映射会话: {endpoint}", flush=True)
    state.client.unassign(endpoint)
    for attempt in range(5):
        remaining_endpoints = {
            assignment.endpoint for assignment in state.client.list_assignments()
        }
        if endpoint not in remaining_endpoints:
            print(f">>> 未映射 Runtime 已停止: {endpoint}", flush=True)
            return
        if attempt < 4:
            time.sleep(1)
    raise RuntimeError(f"Runtime 停止请求已发送，但它仍处于活动状态: {endpoint}")
