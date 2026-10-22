"""Transport 抽象：隔离 Colab 私有 API，支撑 native/cli 双通道与降级。

接口方法与 doc/05 P0.2 一致（exec_script / exec_workflow /
download_output / upload / session_status），外加编排必需的
create_session / mount_drive / stop_session / check_session_readable。

事件回调 `sink(phase, status, message, data)` 的 phase/status 取值与
colab_core.events / contracts/task_event.schema.json 对齐；为保持
`colab_core → colab_transport` 的单向依赖，本层使用字符串字面量。
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

EventSink = Callable[[str, str, str, Optional[dict]], None]


class TransportError(RuntimeError):
    """通道层用户可读失败（子命令非零退出、会话不可用等）。

    colab_core 边界会转换为 TaskError，CLI 输出与旧 sys.exit 文案一致。
    """


def _null_sink(phase: str, status: str, message: str, data: Any = None) -> None:
    if message:
        print(message, flush=True)


class Transport(ABC):
    """Colab 通道抽象。`mode` 取值 native | cli。"""

    mode: str = "cli"

    @abstractmethod
    def create_session(self, session: str, kind: str, spec: str) -> None:
        """创建新 VM（colab new）。"""

    @abstractmethod
    def mount_drive(self, session: str) -> bool:
        """挂载 Google Drive；返回是否成功，失败仍不致命。"""

    @abstractmethod
    def stop_session(self, session: str) -> None:
        """销毁会话；失败不致命（旧行为 check=False）。"""

    @abstractmethod
    def session_status(self, session: str) -> str:
        """返回 colab status 的合并输出文本（含 Variant:/Status: 行）。"""

    @abstractmethod
    def check_session_readable(self, session: str) -> None:
        """可用性检查（colab ls /content）；不可复用时抛 TransportError。"""

    @abstractmethod
    def upload(self, session: str, local_path, remote_path: str) -> None:
        """上传单个文件到会话。"""

    @abstractmethod
    def exec_script(
        self,
        session: str,
        script_path: Path,
        timeout: float,
        sink: EventSink = _null_sink,
    ) -> None:
        """执行远端脚本（remote_setup），输出经 sink 逐行转发。"""

    @abstractmethod
    def exec_workflow(
        self,
        session: str,
        script_path: Path,
        timeout: float,
        output_zip: Path,
        sink: EventSink = _null_sink,
    ) -> bool:
        """执行远端工作流并在任务退出前完成结果交接。

        返回 True 表示输出已通过执行通道/实时下载完整取回；
        返回 False 表示需要调用方走 download_output 兜底。
        """

    @abstractmethod
    def download_output(self, session: str, output_zip: Path,
                        sink: EventSink = _null_sink) -> None:
        """任务结束后从会话取回输出分块并重组校验。"""

    def remote_output_ready(self, session: str) -> Optional[bool]:
        """远端产物清单（manifest.json）是否已生成；None = 本通道不支持探测。

        供执行阶段断线后的 reattach 轮询使用（colab_core.resilience）。
        """
        return None
