"""TaskEvent 事件协议（contracts/task_event.schema.json 的 Python 对齐）。

`colab_core.run_task()` 通过注入的 `emit(event)` 产出事件：
CLI 模式下 `make_cli_emit()` 原样打印中文消息（保持 start.sh 可用），
服务模式下由 Runner 推 SSE 队列并落盘 events.jsonl。

约定：
- `message` 非空 → CLI 直接打印；空串 → 仅服务端按 phase/data 渲染。
- `metrics.sample` / `log.line` 以及 `status == "progress"` 只走 SSE，不入库。
"""
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypedDict

PROTOCOL_VERSION = 1

# 与 contracts/task_event.schema.json 的 phase 枚举保持一致
PHASE_TASK_QUEUED = "task.queued"
PHASE_TASK_START = "task.start"
PHASE_SESSION_CREATE = "session.create"
PHASE_SESSION_REUSE = "session.reuse"
PHASE_DRIVE_MOUNT = "drive.mount"
PHASE_INPUT_RESOLVE = "input.resolve"
PHASE_INPUT_UPLOAD = "input.upload"
PHASE_ENV_SETUP = "env.setup"
PHASE_MODEL_PREPARE = "model.prepare"
PHASE_NODE_PREPARE = "node.prepare"
PHASE_COMFY_START = "comfy.start"
PHASE_WORKFLOW_EXEC = "workflow.exec"
PHASE_OUTPUT_PACK = "output.pack"
PHASE_OUTPUT_TRANSFER = "output.transfer"
PHASE_SESSION_STOP = "session.stop"
PHASE_SESSION_KEEP = "session.keep"
PHASE_TASK_DONE = "task.done"
PHASE_TASK_FAILED = "task.failed"
PHASE_TASK_CANCELED = "task.canceled"
PHASE_METRICS_SAMPLE = "metrics.sample"
PHASE_LOG_LINE = "log.line"

PHASES = (
    PHASE_TASK_QUEUED,
    PHASE_TASK_START,
    PHASE_SESSION_CREATE,
    PHASE_SESSION_REUSE,
    PHASE_DRIVE_MOUNT,
    PHASE_INPUT_RESOLVE,
    PHASE_INPUT_UPLOAD,
    PHASE_ENV_SETUP,
    PHASE_MODEL_PREPARE,
    PHASE_NODE_PREPARE,
    PHASE_COMFY_START,
    PHASE_WORKFLOW_EXEC,
    PHASE_OUTPUT_PACK,
    PHASE_OUTPUT_TRANSFER,
    PHASE_SESSION_STOP,
    PHASE_SESSION_KEEP,
    PHASE_TASK_DONE,
    PHASE_TASK_FAILED,
    PHASE_TASK_CANCELED,
    PHASE_METRICS_SAMPLE,
    PHASE_LOG_LINE,
)

STATUS_START = "start"
STATUS_PROGRESS = "progress"
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_ERROR = "error"
STATUSES = (
    STATUS_START,
    STATUS_PROGRESS,
    STATUS_OK,
    STATUS_WARN,
    STATUS_ERROR,
)

TERMINAL_PHASES = (PHASE_TASK_DONE, PHASE_TASK_FAILED, PHASE_TASK_CANCELED)

# 入库规则（doc/02 §1.1）：这些 phase/status 只走 SSE，不进 TaskEvent 表，
# 也不写 events.jsonl（metrics/log 走 run.log）。
SSE_ONLY_PHASES = frozenset({PHASE_METRICS_SAMPLE, PHASE_LOG_LINE})


class TaskEvent(TypedDict, total=False):
    """事件信封；与 contracts/task_event.schema.json 对齐。"""

    v: int
    task_id: str
    seq: int
    ts: str
    phase: str
    status: str
    message: str
    data: dict[str, Any]


class InputFileEntry(TypedDict):
    workflow_path: str
    local_path: str
    remote_path: str


class TaskFailedData(TypedDict, total=False):
    error_type: str
    message: str
    session_kept: bool
    recovery_hint: str


EmitCallable = Callable[[TaskEvent], None]


def utc_now_iso() -> str:
    """UTC ISO8601 毫秒精度时间戳（与契约 ts 字段一致）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class EventEmitter:
    """为事件补齐信封字段（v/task_id/seq/ts）并追踪当前阶段。

    `sink` 接收完整事件字典；`seq` 从 0 开始严格自增。
    """

    def __init__(self, task_id: str, sink: EmitCallable) -> None:
        self._task_id = task_id
        self._sink = sink
        self._seq = -1
        self.current_phase: Optional[str] = None

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(
        self,
        phase: str,
        status: str,
        message: str = "",
        data: Optional[dict[str, Any]] = None,
    ) -> TaskEvent:
        """构造并发出一个事件；返回事件字典。"""
        if phase not in PHASES:
            raise ValueError(f"未知事件 phase: {phase!r}")
        if status not in STATUSES:
            raise ValueError(f"未知事件 status: {status!r}")
        self._seq += 1
        event: TaskEvent = {
            "v": PROTOCOL_VERSION,
            "task_id": self._task_id,
            "seq": self._seq,
            "ts": utc_now_iso(),
            "phase": phase,
            "status": status,
            "message": message,
            "data": dict(data) if data else {},
        }
        if phase not in (PHASE_METRICS_SAMPLE, PHASE_LOG_LINE):
            self.current_phase = phase
        self._sink(event)
        return event

    def log(self, text: str, stream: str = "stdout") -> TaskEvent:
        """透传一行原始输出为 log.line 事件（仅 SSE / 落 run.log）。"""
        return self.emit(
            PHASE_LOG_LINE,
            STATUS_PROGRESS,
            text,
            {"stream": stream, "text": text},
        )


def make_cli_emit() -> EmitCallable:
    """CLI 模式 emit：把事件 message 打印成与旧 run.py 逐字一致的输出。

    - 空 message（仅服务端里程碑）不打印；
    - stderr 流与 error 状态写到 stderr。
    """

    def emit(event: TaskEvent) -> None:
        message = event.get("message") or ""
        if not message:
            return
        data = event.get("data") or {}
        if event.get("status") == STATUS_ERROR or data.get("stream") == "stderr":
            print(message, file=sys.stderr, flush=True)
        else:
            print(message, flush=True)

    return emit
