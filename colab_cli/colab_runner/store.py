"""事件存储与分发：每任务一个 EventLog。

职责（doc/02 §2.5/§2.6 的硬性要求）：

- **seq 重盖**：core 的 EventEmitter 从 0 开始发 seq；Runner 在受理时还要先
  发一个 core 不知道的 task.queued。这里统一由 EventLog 持有每任务 seq 计数器，
  一切事件（Runner 自产的与 core 发来的）进 `emit()` 后重盖 seq ——
  task.queued 占 seq=0，core 的第 N 个事件落成 seq=N+1。契约要求「单任务内
  从 0 自增、严格单调」，重盖后天然满足，且不依赖 core 的计数起点。
- **events.jsonl 落盘**：追加写 + 每事件 fsync。`metrics.sample` / `log.line`
  与一切 `status == "progress"` 的事件**不写**（否则文件会爆）。
- **内存 ring buffer**（最近 2000 条已落盘事件）只是读缓存；`from_seq` 落在
  窗口外时回文件读（Nitro 宕机 1 小时 ring 早滚掉了，终态事件不能丢）。
- **run.log**：`log.line` 逐行落盘；64 MiB 硬上限，超过则滚动为 run.log.1
  （保留 1 份），截断时向事件流补一个 `status="warn"` 事件。
- **SSE 分发**：在线订阅者收全部事件；progress 类（log.line 除外）按 phase
  1 秒节流 —— 丢弃中间帧，不是延迟全部发；节流只影响 SSE 推送，不影响
  seq 分配（被丢的帧仍占 seq，SSE 流里允许跳号，from_seq 语义不受影响）。
"""
import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from colab_core import events as ev

RUN_LOG_MAX_BYTES = 64 * 1024 * 1024  # doc/02 §2.6：run.log 硬上限 64 MiB
RING_BUFFER_SIZE = 2000               # doc/02 §2.5：内存 ring buffer 容量
THROTTLE_SECONDS = 1.0                # doc/02 §2.6：progress/metrics 按 1 秒节流
SUBSCRIBER_QUEUE_SIZE = 4096          # 慢消费者保护：积满即断开，由 Console 重连补拉

# 终态事件之后允许继续 append（redownload 会在 task.failed 之后补 output.transfer
# 与第二个 task.done），所以文件句柄只在 close() 时真正关闭。


def build_event(task_id: str, phase: str, status: str,
                message: str = "", data: Optional[dict] = None) -> dict:
    """构造 Runner 自产事件的信封（seq 占位 -1，进 EventLog.emit 时重盖）。"""
    return {
        "v": ev.PROTOCOL_VERSION,
        "task_id": task_id,
        "seq": -1,
        "ts": ev.utc_now_iso(),
        "phase": phase,
        "status": status,
        "message": message,
        "data": dict(data) if data else {},
    }


def is_persisted(event: dict) -> bool:
    """入库/落盘规则（doc/02 §1.1、§2.5）：里程碑才写 events.jsonl。"""
    return (
        event["phase"] not in ev.SSE_ONLY_PHASES
        and event["status"] != ev.STATUS_PROGRESS
    )


class EventLog:
    """单任务的事件路由中心。线程安全：全部 public 方法内部取同一把锁。

    `on_persisted(event)` 回调在每个落盘事件写完后触发（持锁状态、任务线程上），
    供 TaskManager 更新任务元数据与 runner_state.json。
    """

    def __init__(
        self,
        task_id: str,
        out_dir: Path,
        on_persisted: Optional[Callable[[dict], None]] = None,
        start_seq: int = -1,
    ) -> None:
        self.task_id = task_id
        self.out_dir = Path(out_dir)
        self.events_path = self.out_dir / "events.jsonl"
        self.run_log_path = self.out_dir / "run.log"
        self._on_persisted = on_persisted
        self._lock = threading.Lock()
        # start_seq：从磁盘水合时传入 events.jsonl 最后一条的 seq，redownload 继续递增
        self._seq = start_seq
        self._ring: deque[dict] = deque(maxlen=RING_BUFFER_SIZE)
        self._subscribers: list[queue.Queue] = []
        self._throttle_last: dict[str, float] = {}
        self._events_file = None
        self._run_log_file = None
        self._run_log_bytes: Optional[int] = None
        self.current_phase: Optional[str] = None

    @property
    def last_seq(self) -> int:
        return self._seq

    # ── 事件入口 ─────────────────────────────────────────────

    def emit(self, event: dict) -> dict:
        """路由一个事件（core sink 与 Runner 自产事件共用）。返回重盖 seq 后的事件。"""
        with self._lock:
            self._seq += 1
            event = {**event, "seq": self._seq, "task_id": self.task_id}

            persisted = is_persisted(event)
            if persisted:
                self._append_events_file(event)
                self._ring.append(event)
                if event["phase"] not in ev.SSE_ONLY_PHASES:
                    self.current_phase = event["phase"]

            if event["phase"] == ev.PHASE_LOG_LINE:
                self._append_run_log(event)

            if self._should_push(event):
                self._push_locked(event)

            if persisted and self._on_persisted is not None:
                self._on_persisted(event)

            if event["phase"] in ev.TERMINAL_PHASES:
                # 终态后流关闭（doc/02 §2.5）：终态事件送达后给所有订阅者收尾。
                self._push_locked(None)
                self._subscribers.clear()
            return event

    # ── SSE 订阅 ─────────────────────────────────────────────

    def subscribe(self, from_seq: int) -> tuple[list[dict], int, queue.Queue]:
        """返回 (需回放的已落盘事件, 水位 seq, 在线队列)。

        持锁完成「快照 + 挂队列」，保证两者之间不丢事件：在线队列里 seq <= 水位
        的事件由消费端跳过（回放只含落盘事件，水位按全部事件计，避免把注册前
        的 progress/metrics 当新事件重发）。
        """
        subscriber: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            replay = self._replay_locked(from_seq)
            high_water = self._seq
            self._subscribers.append(subscriber)
        return replay, high_water, subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def replay(self, from_seq: int) -> list[dict]:
        """seq > from_seq 的已落盘事件（终态任务的 SSE 回放用）。"""
        with self._lock:
            return self._replay_locked(from_seq)

    def _replay_locked(self, from_seq: int) -> list[dict]:
        """seq > from_seq 的全部已落盘事件；ring 窗口不够时回文件读。"""
        if self._ring and from_seq + 1 >= self._ring[0]["seq"]:
            return [event for event in self._ring if event["seq"] > from_seq]
        return read_events_file(self.events_path, from_seq)

    def _should_push(self, event: dict) -> bool:
        """SSE 推送闸门：progress 类（log.line 除外）按 phase 1 秒节流。"""
        if (
            event["status"] != ev.STATUS_PROGRESS
            or event["phase"] == ev.PHASE_LOG_LINE
        ):
            return True
        now = time.monotonic()
        last = self._throttle_last.get(event["phase"])
        if last is not None and now - last < THROTTLE_SECONDS:
            return False  # 丢弃中间帧；事件已占 seq，但不推 SSE、不落盘
        self._throttle_last[event["phase"]] = now
        return True

    def _push_locked(self, item) -> None:
        for subscriber in list(self._subscribers):
            try:
                subscriber.put_nowait(item)
            except queue.Full:
                # 慢消费者：断开让它走 from_seq 重连补拉，不能拖垮任务线程。
                self._subscribers.remove(subscriber)

    # ── 落盘 ─────────────────────────────────────────────────

    def _ensure_events_file(self):
        if self._events_file is None:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._events_file = self.events_path.open("a", encoding="utf-8")
        return self._events_file

    def _append_events_file(self, event: dict) -> None:
        file = self._ensure_events_file()
        file.write(json.dumps(event, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())

    def _append_run_log(self, event: dict) -> None:
        text = (event.get("data") or {}).get("text") or event.get("message") or ""
        line = text + "\n"
        encoded_size = len(line.encode("utf-8"))
        if self._run_log_bytes is None:
            self._run_log_bytes = (
                self.run_log_path.stat().st_size
                if self.run_log_path.exists()
                else 0
            )
        if (
            self._run_log_bytes > 0
            and self._run_log_bytes + encoded_size > RUN_LOG_MAX_BYTES
        ):
            self._rotate_run_log_locked()
        if self._run_log_file is None:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._run_log_file = self.run_log_path.open("a", encoding="utf-8")
        self._run_log_file.write(line)
        self._run_log_file.flush()
        self._run_log_bytes += encoded_size

    def _rotate_run_log_locked(self) -> None:
        """64 MiB 上限：滚动为 run.log.1（保留 1 份），并补一个 warn 事件。"""
        if self._run_log_file is not None:
            self._run_log_file.close()
            self._run_log_file = None
        backup = self.run_log_path.with_name("run.log.1")
        os.replace(self.run_log_path, backup)
        self._run_log_bytes = 0
        warn = build_event(
            self.task_id,
            self.current_phase or ev.PHASE_TASK_START,
            ev.STATUS_WARN,
            ">>> run.log 超过 64 MiB，已滚动为 run.log.1（仅保留 1 份旧日志）",
            {
                "reason": "run_log_rotated",
                "limit_bytes": RUN_LOG_MAX_BYTES,
                "backup": str(backup),
            },
        )
        # 不走 self.emit 递归（warn 不是 log.line，不会再触发轮转），手动走一遍
        # 落盘 + 推送路径。
        self._seq += 1
        warn = {**warn, "seq": self._seq, "task_id": self.task_id}
        self._append_events_file(warn)
        self._ring.append(warn)
        self._push_locked(warn)
        if self._on_persisted is not None:
            self._on_persisted(warn)

    def close(self) -> None:
        with self._lock:
            if self._events_file is not None:
                self._events_file.close()
                self._events_file = None
            if self._run_log_file is not None:
                self._run_log_file.close()
                self._run_log_file = None


def read_events_file(events_path: Path, from_seq: int = -1) -> list[dict]:
    """从 events.jsonl 读 seq > from_seq 的事件。

    容忍末行截断（Runner 崩溃时可能写了一半）：解析失败的行跳过。
    """
    events: list[dict[str, Any]] = []
    path = Path(events_path)
    if not path.is_file():
        return events
    with path.open(encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("seq", -1) > from_seq:
                events.append(event)
    return events
