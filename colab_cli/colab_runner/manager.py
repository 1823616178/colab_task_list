"""TaskManager：任务生命周期、session 互斥、取消、崩溃恢复。

线程模型（doc/02 §2.3.2 的硬约束）：

- `kernel_client.execute_interactive` / `subprocess.run` 都是**同步阻塞**，
  所以任务在 `ThreadPoolExecutor` 的工作线程里同步跑 `run_task`，
  ASGI 层只负责收发，阻塞调用绝不进 asyncio 事件循环。
- **Python 杀不了阻塞线程**，取消 = 两件真实手段的组合：
  1. 置 `cancel_requested` 标志 —— Runner 的 core sink 在**下一次 emit** 时
     抛 `RunnerTaskCanceled`，让任务线程尽快退出收尾；
  2. 守护线程里向 Colab 下发 `colab restart-kernel`（interrupt）或
     `colab stop`（destroy），打断正在阻塞的内核执行。
  cancel 期间 core 自己补发的 task.failed/task.done 是「伪终态」，被 sink
  吞掉；唯一的 task.canceled 终态事件由任务线程统一发出。
- queued（已受理、还没进 run_task）取消：直接置终态，不碰 VM。

崩溃恢复（doc/03 §1）：只维护 `runner_state.json`，记录在飞任务
{session, pid, started_at, last_seq, phase}；重启后这些任务的线程已丢失，
标记为 interrupted 交给 Console 对账（VM 可能仍在跑），并继续持有其
session 锁，直到 `DELETE /sessions/{name}` 显式解除。
"""
import json
import os
import subprocess
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from colab_core import TaskConfig, TaskError, create_task_id, run_task
from colab_core import events as ev
from colab_core.paths import HERE, default_output_root
from colab_transport import TransportError, get_transport
from colab_transport.cli import (
    ColabCli,
    cancel_drive_mount,
    confirm_drive_mount,
)
from colab_transport.transfer import file_sha256

from .store import EventLog, build_event, read_events_file

TERMINAL_STATUSES = ("succeeded", "failed", "canceled")
# redownloading / interrupted 之外的一切在飞状态都占 session 锁
LIVE_STATUSES = ("queued", "running", "redownloading", "interrupted")

CANCEL_MODES = ("interrupt", "destroy")


class ManagerError(RuntimeError):
    """Runner 业务错误；app 层映射为 doc/02 §3.4 的统一错误格式。"""

    http_status = 500
    code = "RUNNER_ERROR"

    def __init__(self, message: str, detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


class SessionBusyError(ManagerError):
    http_status = 409
    code = "SESSION_BUSY"


class SessionQueueFullError(SessionBusyError):
    code = "SESSION_QUEUE_FULL"


def _queue_max() -> int:
    """每个会话的排队上限（不含正在运行的那个）；COLAB_SESSION_QUEUE_MAX，缺省 5，0=禁用排队。"""
    try:
        return max(0, int(os.environ.get("COLAB_SESSION_QUEUE_MAX", "5")))
    except ValueError:
        return 5


class TaskNotFoundError(ManagerError):
    http_status = 404
    code = "TASK_NOT_FOUND"


class TaskTerminalError(ManagerError):
    http_status = 409
    code = "TASK_FINISHED"


class RedownloadNotAllowedError(ManagerError):
    http_status = 409
    code = "REDOWNLOAD_NOT_ALLOWED"


class DriveConfirmationUnavailableError(ManagerError):
    http_status = 409
    code = "DRIVE_CONFIRMATION_UNAVAILABLE"


class BadTaskConfigError(ManagerError):
    http_status = 400
    code = "BAD_REQUEST"


class RunnerTaskCanceled(Exception):
    """sink 内抛出，让任务线程在下一次 emit 时尽快退出（不是真实故障）。"""


def _deliver_cancel(session: str, mode: str) -> tuple[bool, str]:
    """向 Colab 下发取消指令：interrupt=restart-kernel，destroy=stop。

    模块级函数（不是方法），方便测试 monkeypatch —— 单测里没有真实 VM。
    """
    cli = ColabCli(session)
    verb = "restart-kernel" if mode == "interrupt" else "stop"
    command = cli.build(verb, "-s", session)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=180, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    output = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
    return result.returncode == 0, output


@dataclass
class TaskRecord:
    """内存中的任务档案；终态事实以 events.jsonl 为准。"""

    task_id: str
    session: str
    out_dir: Path
    cfg: Optional[TaskConfig] = None
    status: str = "queued"
    queued_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    current_phase: Optional[str] = None
    last_seq: int = -1
    terminal_phase: Optional[str] = None
    error: Optional[dict] = None
    session_kept: bool = False
    cancel_requested: bool = False
    cancel_mode: Optional[str] = None
    recovered: bool = False  # True = Runner 重启后从 runner_state.json 恢复的对账项
    transport_mode: Optional[str] = None
    duration_ms: Optional[int] = None
    output_zip: Optional[Path] = None
    output_size_bytes: Optional[int] = None
    output_sha256: Optional[str] = None
    event_log: Optional[EventLog] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def terminal(self) -> bool:
        return self.terminal_phase is not None

    @property
    def recoverable(self) -> bool:
        """失败/取消但会话保留：远端可能仍有成品，允许 redownload。"""
        return self.status in ("failed", "canceled") and self.session_kept


class TaskManager:
    """任务的受理、执行、取消、恢复；一切状态只在内存与本地文件。"""

    def __init__(
        self,
        output_root: Optional[Path] = None,
        max_workers: int = 4,
        state_file: Optional[Path] = None,
    ) -> None:
        self.output_root = Path(output_root or default_output_root())
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.state_path = Path(
            state_file
            or os.environ.get("COLAB_RUNNER_STATE_FILE")
            or self.output_root / "runner_state.json"
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="colab-task"
        )
        self._lock = threading.RLock()  # 可重入：submit 持锁 emit 会经回调写 state 文件；护 registry / session map / state 写
        self._records: dict[str, TaskRecord] = {}
        self._session_owner: dict[str, str] = {}  # session -> task_id（非终态）
        # session -> 排队中的 task_id（FIFO）。不变式：队列非空 ⇒ 该会话有 owner；
        # owner 释放时由 _release_session 立即把队首提升为 owner 并投入执行。
        self._session_queue: dict[str, deque[str]] = {}
        self.recovered_inflight: list[str] = []

    # ── 受理 ─────────────────────────────────────────────────

    def submit(self, payload: dict) -> TaskRecord:
        """POST /tasks：校验、占 session 锁、发 task.queued、投后台线程。"""
        cfg = self._parse_config(payload)
        allow_queue = payload.get("queue", True) is not False
        with self._lock:
            owner = self._session_owner.get(cfg.session)
            enqueue = False
            if owner is not None:
                self._check_can_enqueue(cfg, owner, allow_queue)
                enqueue = True
            task_id = create_task_id()
            out_dir = self.output_root / task_id
            out_dir.mkdir(parents=True, exist_ok=True)
            # 任务目录（含 events.jsonl/run.log/快照/output.zip）由 Runner 定，
            # core 的一切产物落进同一个目录（doc/02 §2.3.1 布局）
            cfg.task_id = task_id
            cfg.output_dir = out_dir
            record = TaskRecord(
                task_id=task_id,
                session=cfg.session,
                out_dir=out_dir,
                cfg=cfg,
                queued_at=ev.utc_now_iso(),
            )
            record.event_log = EventLog(
                task_id, out_dir, on_persisted=self._make_on_persisted(record)
            )
            self._records[task_id] = record
            position = 0
            if enqueue:
                queue = self._session_queue.setdefault(cfg.session, deque())
                queue.append(task_id)
                position = len(queue)
            else:
                self._session_owner[cfg.session] = task_id
            # task.queued 由 Runner 发（core 不发这个），占 seq=0（见 store.py 说明）。
            # 在 self._lock 内发：保证 queued 事件先于提升执行后的 task.start 落盘
            record.event_log.emit(build_event(
                task_id,
                ev.PHASE_TASK_QUEUED,
                ev.STATUS_START,
                (f"任务已受理: {task_id}（会话 {cfg.session} 排队第 {position} 位）"
                 if position else f"任务已受理: {task_id}"),
                {"session": cfg.session,
                 "workflow_config": str(cfg.workflow_config),
                 "dry_run": cfg.dry_run,
                 "queue_position": position},
            ))
        if not enqueue:
            self._executor.submit(self._execute, record)
        return record

    def _check_can_enqueue(self, cfg: TaskConfig, owner: str, allow_queue: bool) -> None:
        """同会话已有任务时能否排队（调用方须持 self._lock）；不能则抛 409。"""
        busy_detail = {"session": cfg.session, "running_task_id": owner}
        owner_record = self._records.get(owner)
        if not allow_queue or _queue_max() == 0:
            raise SessionBusyError(f"会话 {cfg.session} 仍有任务运行", busy_detail)
        if owner_record is None or owner_record.status == "interrupted":
            # Runner 重启后的对账项：VM 状态未知，不能往后排
            raise SessionBusyError(
                f"会话 {cfg.session} 有待对账的中断任务，请先在会话页处理", busy_detail)
        queue = self._session_queue.get(cfg.session) or deque()
        if len(queue) >= _queue_max():
            raise SessionQueueFullError(
                f"会话 {cfg.session} 排队已满（上限 {_queue_max()}）",
                {**busy_detail, "queue_length": len(queue)},
            )
        # 前一个任务 keep=True 会留下同名 VM；新任务若不是复用模式会新建同名 VM，
        # 把旧 VM 变成名称丢失的计费孤儿 —— 必须显式 reuse 才能排在它后面
        predecessor = self._records.get(queue[-1]) if queue else owner_record
        if (predecessor is not None and predecessor.cfg is not None
                and predecessor.cfg.keep and not cfg.reuse and not cfg.dry_run):
            raise SessionBusyError(
                f"会话 {cfg.session} 的前序任务会保留 VM，排队任务须开启「复用会话」",
                busy_detail,
            )

    def queue_position(self, record: TaskRecord) -> Optional[int]:
        """排队中的任务返回 1 起的位置；正在运行/未排队返回 None。"""
        queue = self._session_queue.get(record.session)
        if not queue or record.task_id not in queue:
            return None
        return list(queue).index(record.task_id) + 1

    def session_queue(self, session: str) -> list[str]:
        return list(self._session_queue.get(session) or ())

    def _parse_config(self, payload: dict) -> TaskConfig:
        if not isinstance(payload, dict):
            raise BadTaskConfigError("请求体必须是 JSON 对象")
        anchored = dict(payload)
        for key in ("workflow_config", "api_json", "input_dir"):
            value = anchored.get(key)
            if isinstance(value, str) and value and not Path(value).is_absolute():
                anchored[key] = str(HERE / value)  # 相对路径锚定仓库根
        try:
            cfg = TaskConfig.from_dict(anchored)
        except ValueError as error:  # 未知加速器类型/规格
            bad_config = BadTaskConfigError(str(error))
            bad_config.code = "ACCELERATOR_UNSUPPORTED"
            raise bad_config from error
        except (KeyError, TypeError) as error:
            raise BadTaskConfigError(f"请求体字段缺失或类型错误: {error}") from error
        return cfg

    # ── 任务线程 ─────────────────────────────────────────────

    def _make_on_persisted(self, record: TaskRecord):
        """落盘事件回调：更新档案元数据 + runner_state.json（在任务线程上）。"""

        def on_persisted(event: dict) -> None:
            record.last_seq = event["seq"]
            record.current_phase = event["phase"]
            if event["phase"] == ev.PHASE_TASK_FAILED:
                record.error = event.get("data") or {}
                record.session_kept = bool(record.error.get("session_kept"))
            if event["phase"] in ev.TERMINAL_PHASES:
                record.terminal_phase = event["phase"]
                record.finished_at = event["ts"]
            self._sync_state_file()

        return on_persisted

    def _make_core_emit(self, record: TaskRecord):
        """交给 run_task 的 emit：cancel 检查 + 事件路由。"""

        def core_emit(event: dict) -> None:
            if record.cancel_requested:
                # 吞掉 core 的伪终态；唯一的 task.canceled 由 _execute 收尾发
                if event["phase"] in ev.TERMINAL_PHASES:
                    return
                raise RunnerTaskCanceled
            record.event_log.emit(event)

        return core_emit

    def _execute(self, record: TaskRecord) -> None:
        with record.lock:
            if record.terminal:
                return  # queued 期间已被取消并收尾
            record.status = "running"
            record.started_at = ev.utc_now_iso()
            # 状态落文件与内存赋值在同一临界区：快照读到 running 时，
            # runner_state.json 必然已含 started_at（无竞态窗口）
            self._sync_state_file()
        result = None
        unexpected: Optional[Exception] = None
        try:
            result = run_task(record.cfg, self._make_core_emit(record))
        except RunnerTaskCanceled:
            pass
        except (TaskError, TransportError):
            pass  # task.failed 事件已由 core 发出并经回调记录
        except Exception as error:  # 意外异常：防御性补终态
            unexpected = error
        finally:
            try:
                with record.lock:
                    if record.cancel_requested and not record.terminal:
                        self._finalize_cancel_locked(record)
                    if record.terminal_phase == ev.PHASE_TASK_DONE:
                        record.status = "succeeded"
                        if result is not None:
                            record.output_zip = result.output_zip
                            record.output_size_bytes = result.output_size_bytes
                            record.output_sha256 = result.output_sha256
                            record.transport_mode = result.transport_mode
                            record.duration_ms = result.duration_ms
                            record.session_kept = result.session_kept
                    elif record.terminal_phase == ev.PHASE_TASK_FAILED:
                        record.status = "failed"
                    elif record.terminal_phase == ev.PHASE_TASK_CANCELED:
                        record.status = "canceled"
                    elif unexpected is not None:
                        # core 未能发出终态事件（防御）：由 Runner 补发
                        record.event_log.emit(build_event(
                            record.task_id,
                            ev.PHASE_TASK_FAILED,
                            ev.STATUS_ERROR,
                            "",
                            {
                                "error_type": type(unexpected).__name__,
                                "message": str(unexpected),
                                "session_kept": False,
                                "recovery_hint": "",
                            },
                        ))
                        record.status = "failed"
            finally:
                self._release_session(record)
                self._sync_state_file()
                record.event_log.close()

    # ── 取消 ─────────────────────────────────────────────────

    def cancel(self, task_id: str, mode: str) -> str:
        """返回 "canceled"（queued 直接收尾）或 "canceling"（running 异步）。"""
        record = self._records.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"任务不存在: {task_id}")
        if mode not in CANCEL_MODES:
            raise BadTaskConfigError(
                f"未知取消模式: {mode!r}（可选: interrupt | destroy）"
            )
        with record.lock:
            if record.terminal:
                raise TaskTerminalError(f"任务已结束: {record.status}")
            if record.status == "interrupted":
                raise TaskTerminalError(
                    "任务是 Runner 重启后的对账项，线程已丢失；"
                    "请用 DELETE /sessions 处理对应会话"
                )
            record.cancel_requested = True
            record.cancel_mode = mode
            # Drive OAuth 会在本地伪终端阻塞等待 Enter。仅下发远端 stop/restart
            # 无法保证解除它（尤其名称映射已经丢失时），必须先终止本地等待。
            waiting_in_queue = self.queue_position(record) is not None
            if not waiting_in_queue:
                # 排队中的任务还没碰会话：不能误杀前序任务的 Drive 授权等待
                cancel_drive_mount(record.session)
            if record.status == "queued":
                # 尚未进 run_task：直接置终态，不碰 VM（doc/02 §2.3.2）。
                # 任务线程稍后被 executor 拾取时看到终态会直接返回。
                self._finalize_cancel_locked(record)
                self._release_session(record)
                self._sync_state_file()
                record.event_log.close()
                return "canceled"
        # running：守护线程下发取消指令（restart-kernel / stop），
        # 任务线程在 sink 下一次 emit 或阻塞返回后收尾。
        threading.Thread(
            target=self._deliver_cancel_async,
            args=(record,),
            daemon=True,
            name=f"cancel-{task_id}",
        ).start()
        return "canceling"

    def _deliver_cancel_async(self, record: TaskRecord) -> None:
        ok, output = _deliver_cancel(record.session, record.cancel_mode)
        if ok:
            return
        with record.lock:
            if record.terminal:
                return
            record.event_log.emit(build_event(
                record.task_id,
                record.current_phase or ev.PHASE_TASK_START,
                ev.STATUS_WARN,
                f">>> 取消指令下发失败（{record.cancel_mode}），任务可能仍在运行",
                {"reason": "cancel_delivery_failed",
                 "mode": record.cancel_mode, "detail": output[:500]},
            ))

    def confirm_drive(self, task_id: str) -> str:
        """确认浏览器授权已完成，让 drivemount 的伪终端继续执行。"""
        record = self.get(task_id)
        with record.lock:
            if record.terminal or record.status != "running":
                raise TaskTerminalError(f"任务已结束或未运行: {record.status}")
            if record.current_phase != ev.PHASE_DRIVE_MOUNT:
                raise DriveConfirmationUnavailableError(
                    "任务当前不在等待 Google Drive 授权"
                )
            session = record.session
        if not confirm_drive_mount(session):
            raise DriveConfirmationUnavailableError(
                "Google Drive 授权确认窗口尚未就绪或已经关闭"
            )
        return session

    def _finalize_cancel_locked(self, record: TaskRecord) -> None:
        """发唯一的 task.canceled 终态事件（调用方须持 record.lock）。"""
        mode = record.cancel_mode or "interrupt"
        interrupt = mode == "interrupt" and record.status != "queued"
        # interrupt：VM 保留、模型仍在磁盘、可立即重投，远端可能已有部分产物
        # → recoverable 语义（doc/02 §2.3.2）；destroy / queued 均不可恢复。
        record.session_kept = interrupt
        record.event_log.emit(build_event(
            record.task_id,
            ev.PHASE_TASK_CANCELED,
            ev.STATUS_WARN,
            f"任务已取消（{mode}）",
            {
                "mode": mode,
                "session": record.session,
                "session_kept": interrupt,
                "recoverable": interrupt,
            },
        ))
        record.status = "canceled"

    # ── redownload ───────────────────────────────────────────

    def redownload(self, task_id: str) -> TaskRecord:
        record = self._ensure_live_record(task_id)
        # 终态任务：self._lock → record.lock 安全（_execute 已退出；cancel 对终态直接 409）
        with self._lock:
            with record.lock:
                if not record.recoverable:
                    raise RedownloadNotAllowedError(
                        "仅当任务失败/取消且会话被保留（recoverable）时才允许重取结果",
                        {"status": record.status, "session_kept": record.session_kept},
                    )
                if not record.session:
                    raise RedownloadNotAllowedError(
                        "任务档案中没有会话名，无法重取结果",
                        {"task_id": task_id, "status": record.status},
                    )
                owner = self._session_owner.get(record.session)
                if owner is not None and owner != record.task_id:
                    raise SessionBusyError(
                        f"会话 {record.session} 仍有任务运行",
                        {"session": record.session, "running_task_id": owner},
                    )
                record.status = "redownloading"
                record.terminal_phase = None  # redownload 事件流重新打开
                # 复位取消标志：否则 core_emit 会吞掉 redownload 的全部事件
                # （sink 在 cancel_requested 下对非终态 emit 抛 RunnerTaskCanceled）
                record.cancel_requested = False
                self._ensure_event_log(record)
                self._session_owner[record.session] = record.task_id
        self._executor.submit(self._execute_redownload, record)
        return record

    def _execute_redownload(self, record: TaskRecord) -> None:
        """下载-only：输出到原任务目录，成功后更新 output 路径并补 task.done。"""
        emitter = ev.EventEmitter(record.task_id, self._make_core_emit(record))

        def sink(phase, status, message, data=None):
            emitter.emit(phase, status, message or "", data)

        output_zip = record.out_dir / "output.zip"
        try:
            transport = get_transport(None, sink)
            transport.download_output(record.session, output_zip, sink)
            size_bytes = output_zip.stat().st_size
            sha256 = file_sha256(output_zip)
            emitter.emit(
                ev.PHASE_OUTPUT_TRANSFER,
                ev.STATUS_OK,
                f"结果已下载到: {output_zip}",
                {"local_path": str(output_zip), "size_bytes": size_bytes,
                 "sha256": sha256, "verified": True},
            )
            emitter.emit(
                ev.PHASE_TASK_DONE,
                ev.STATUS_OK,
                "",
                {"task_id": record.task_id, "output_zip": str(output_zip),
                 "size_bytes": size_bytes, "sha256": sha256,
                 "session_kept": True, "transport": transport.mode,
                 "redownload": True},
            )
            with record.lock:
                record.output_zip = output_zip
                record.output_size_bytes = size_bytes
                record.output_sha256 = sha256
                record.transport_mode = transport.mode
                record.session_kept = True
                record.error = None
                record.status = "succeeded"
        except Exception as error:
            emitter.emit(
                ev.PHASE_TASK_FAILED,
                ev.STATUS_ERROR,
                "",
                {"error_type": type(error).__name__, "message": str(error),
                 "session_kept": True,
                 "recovery_hint": "POST /tasks/{id}/redownload"},
            )
            with record.lock:
                record.status = "failed"  # 仍是 recoverable
        finally:
            self._release_session(record)
            self._sync_state_file()
            if record.event_log is not None:
                record.event_log.close()

    # ── 查询 ─────────────────────────────────────────────────

    def get(self, task_id: str) -> TaskRecord:
        record = self._records.get(task_id)
        if record is not None:
            return record
        disk = self._record_from_disk(task_id)
        if disk is not None:
            return disk
        raise TaskNotFoundError(f"任务不存在: {task_id}")

    def peek(self, task_id: str) -> Optional[TaskRecord]:
        """注册表直查（不回落磁盘），供 SSE 端点区分在线/回放。"""
        return self._records.get(task_id)

    def task_id_for_session(self, session: str) -> Optional[str]:
        """该会话当前被哪个非终态任务占用（GET /sessions 标注用）。"""
        return self._session_owner.get(session)

    def running_count(self) -> int:
        return sum(
            1 for record in self._records.values()
            if record.status in ("queued", "running", "redownloading")
        )

    def snapshot(self, record: TaskRecord) -> dict:
        output = None
        if record.output_zip is not None:
            output = {
                "path": str(record.output_zip),
                "size_bytes": record.output_size_bytes,
                "sha256": record.output_sha256,
            }
        return {
            "task_id": record.task_id,
            "status": record.status,
            "session": record.session,
            "phase": record.current_phase,
            "last_seq": record.last_seq,
            "queued_at": record.queued_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "cancel_mode": record.cancel_mode,
            "session_kept": record.session_kept,
            "recoverable": record.recoverable,
            "recovered": record.recovered,
            "dry_run": bool(record.cfg.dry_run) if record.cfg else None,
            "queue_position": self.queue_position(record),
            "transport_mode": record.transport_mode,
            "duration_ms": record.duration_ms,
            "error": record.error,
            "output": output,
        }

    def _ensure_event_log(self, record: TaskRecord) -> None:
        """终态任务 close() 过、或从磁盘水合的档案没有 EventLog：按 last_seq 续接。"""
        if record.event_log is not None:
            return
        record.event_log = EventLog(
            record.task_id,
            record.out_dir,
            on_persisted=self._make_on_persisted(record),
            start_seq=record.last_seq,
        )

    def _ensure_live_record(self, task_id: str) -> TaskRecord:
        """redownload 用：内存档案，必要时从 events.jsonl 水合进 registry。"""
        record = self._records.get(task_id)
        if record is not None:
            return record
        disk = self._record_from_disk(task_id)
        if disk is None:
            raise TaskNotFoundError(f"任务不存在: {task_id}")
        with self._lock:
            existing = self._records.get(task_id)
            if existing is not None:
                return existing
            self._ensure_event_log(disk)
            self._records[task_id] = disk
            return disk

    def _record_from_disk(self, task_id: str) -> Optional[TaskRecord]:
        """注册表未命中时从 events.jsonl 合成快照（Runner 重启后的老任务）。"""
        out_dir = self.output_root / task_id
        events = read_events_file(out_dir / "events.jsonl")
        if not events:
            return None
        record = TaskRecord(
            task_id=task_id,
            session="",
            out_dir=out_dir,
            recovered=True,
        )
        record.queued_at = events[0].get("ts", "")
        record.last_seq = events[-1].get("seq", -1)
        terminal = next(
            (e for e in reversed(events) if e["phase"] in ev.TERMINAL_PHASES),
            None,
        )
        if terminal is None:
            record.status = "interrupted"  # 无终态：Runner 死在了任务中途
        else:
            record.terminal_phase = terminal["phase"]
            record.finished_at = terminal.get("ts")
            record.status = {
                ev.PHASE_TASK_DONE: "succeeded",
                ev.PHASE_TASK_FAILED: "failed",
                ev.PHASE_TASK_CANCELED: "canceled",
            }[terminal["phase"]]
            data = terminal.get("data") or {}
            if terminal["phase"] == ev.PHASE_TASK_DONE:
                output = data.get("output_zip")
                if output:
                    record.output_zip = Path(output)
                    record.output_size_bytes = data.get("size_bytes")
                    record.output_sha256 = data.get("sha256")
                record.session_kept = bool(data.get("session_kept"))
            elif terminal["phase"] == ev.PHASE_TASK_FAILED:
                record.error = data
                record.session_kept = bool(data.get("session_kept"))
            elif terminal["phase"] == ev.PHASE_TASK_CANCELED:
                record.session_kept = bool(data.get("session_kept"))
                record.cancel_mode = data.get("mode")
        for event in reversed(events):
            if event["phase"] not in ev.TERMINAL_PHASES:
                record.current_phase = event["phase"]
                break
        # session 可能只出现在 task.queued / task.start / task.canceled，
        # 不能只看最后一条非终态（output.transfer 通常不带 session）
        for event in reversed(events):
            data = event.get("data") or {}
            if isinstance(data.get("session"), str) and data["session"]:
                record.session = data["session"]
                break
        return record

    # ── 崩溃恢复（runner_state.json）─────────────────────────

    def recover(self) -> None:
        """启动时读 runner_state.json：在飞任务的线程已丢失，标记待对账。"""
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        tasks = state.get("tasks") or {}
        for task_id, info in tasks.items():
            if task_id in self._records:
                continue
            record = TaskRecord(
                task_id=task_id,
                session=info.get("session", ""),
                out_dir=self.output_root / task_id,
                status="interrupted",
                queued_at="",
                started_at=info.get("started_at"),
                current_phase=info.get("phase"),
                last_seq=info.get("last_seq", -1),
                recovered=True,
            )
            self._records[task_id] = record
            if record.session:
                self._session_owner.setdefault(record.session, task_id)
            self.recovered_inflight.append(task_id)

    def release_recovered_session(self, session: str) -> None:
        """DELETE /sessions 后解除该会话上的对账锁（VM 已确认停止）。"""
        with self._lock:
            task_id = self._session_owner.get(session)
            if task_id is None:
                return
            record = self._records.get(task_id)
            if record is not None and record.status == "interrupted":
                self._session_owner.pop(session, None)
                if task_id in self.recovered_inflight:
                    self.recovered_inflight.remove(task_id)
        self._sync_state_file()

    def _release_session(self, record: TaskRecord) -> None:
        """释放会话：owner 释放后把队首（跳过已取消的）提升为 owner 并投入执行；
        排队中被取消的任务只从队列移除。"""
        promoted: Optional[TaskRecord] = None
        with self._lock:
            queue = self._session_queue.get(record.session)
            if queue and record.task_id in queue:
                queue.remove(record.task_id)
            if self._session_owner.get(record.session) == record.task_id:
                self._session_owner.pop(record.session, None)
                while queue:
                    candidate = self._records.get(queue.popleft())
                    if candidate is None or candidate.terminal:
                        continue
                    self._session_owner[record.session] = candidate.task_id
                    promoted = candidate
                    break
            if queue is not None and not queue:
                self._session_queue.pop(record.session, None)
        if promoted is not None:
            self._executor.submit(self._execute, promoted)

    def _sync_state_file(self) -> None:
        """重写 runner_state.json（原子 tmp+replace+fsync）；只记在飞任务。"""
        with self._lock:
            tasks = {}
            for record in self._records.values():
                if record.status not in ("queued", "running", "redownloading"):
                    continue
                tasks[record.task_id] = {
                    "session": record.session,
                    "pid": os.getpid(),
                    "started_at": record.started_at,
                    "last_seq": record.last_seq,
                    "phase": record.current_phase,
                }
            payload = json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2)
            temp_path = self.state_path.with_name(
                f"{self.state_path.stem}.{os.getpid()}.tmp")
            try:
                temp_path.write_text(payload, encoding="utf-8")
                with temp_path.open("rb") as file:
                    os.fsync(file.fileno())
                os.replace(temp_path, self.state_path)
            except OSError:
                pass  # 状态文件只是崩溃恢复辅助，写失败不影响任务本身

    # ── 生命周期 ─────────────────────────────────────────────

    def shutdown(self) -> None:
        # 不等待任务线程（可能跑 24 小时）；在飞任务留在 runner_state.json，
        # 由下次启动的 recover() 标记为待对账。
        self._executor.shutdown(wait=False, cancel_futures=True)
