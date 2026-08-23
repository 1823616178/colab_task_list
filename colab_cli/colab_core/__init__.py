"""colab_core：编排逻辑层。

`run_task(cfg, emit)` 不认识 argparse、不认识 print；CLI（run.py）与
Runner 服务（colab_runner，P1）并列调用，互不感知（doc/06 §2）。
"""
from .config import TaskConfig, TaskResult
from .errors import TaskError
from .events import EventEmitter, TaskEvent, make_cli_emit
from .paths import (
    DEFAULT_WORKFLOW_CONFIG,
    HERE,
    REMOTE_SETUP,
    REMOTE_WORKFLOW,
    default_output_root,
)
from .task import create_task_id, run_task

__all__ = [
    "DEFAULT_WORKFLOW_CONFIG",
    "HERE",
    "REMOTE_SETUP",
    "REMOTE_WORKFLOW",
    "EventEmitter",
    "TaskConfig",
    "TaskError",
    "TaskEvent",
    "TaskResult",
    "create_task_id",
    "default_output_root",
    "make_cli_emit",
    "run_task",
]
