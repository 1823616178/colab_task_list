"""日志配置。用户可见的结果仍用 print 输出到 stdout, 诊断信息走 logging 到 stderr。

每条日志都带 request_id: 网关在处理请求时通过 contextvar 设置, 其余场景为 "-"。
"""

from __future__ import annotations

import contextvars
import logging
import sys
from pathlib import Path

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s"


class RequestIdFilter(logging.Filter):
    """把当前 request_id 注入每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class StderrHandler(logging.StreamHandler):
    """每次写日志时才取 sys.stderr, 避免 stderr 被替换(测试捕获、重定向)后写到已关闭的流。"""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """配置 kaggle_tools 根 logger; 重复调用是幂等的。"""
    logger = logging.getLogger("kaggle_tools")
    logger.setLevel(level.upper())
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handlers: list[logging.Handler] = [StderrHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(RequestIdFilter())
        logger.addHandler(handler)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """取 kaggle_tools 命名空间下的 logger。"""
    return logging.getLogger(name if name.startswith("kaggle_tools") else f"kaggle_tools.{name}")
