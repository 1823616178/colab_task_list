"""colab_transport：Colab 通道适配层。

用法：

    from colab_transport import get_transport

    transport = get_transport()          # 读 COLAB_TRANSPORT，默认 native
    transport = get_transport("cli")     # 强制公共 CLI 通道

私有 API 断裂是高危风险（doc/01 §5 R1），native 模式启用前经
`probe_private_api()` 自检，私有接口不兼容时降级 cli 并告警；
native / cli 共用的内核依赖不兼容时直接报错，避免创建 VM 后才失败。
"""
import os
from typing import Optional

from .base import EventSink, Transport, TransportError, WorkflowNotSubmittedError
from .cli import CliTransport, ColabCli, resolve_config_path
from .native import NativeTransport, OutputStreamReceiver

__all__ = [
    "CliTransport",
    "ColabCli",
    "EventSink",
    "NativeTransport",
    "OutputStreamReceiver",
    "Transport",
    "TransportError",
    "WorkflowNotSubmittedError",
    "colab_cli_version",
    "get_transport",
    "probe_private_api",
    "probe_kernel_client_api",
    "resolve_config_path",
]

TRANSPORT_MODES = ("native", "cli")
BACKENDS = ("colab", "kaggle", "modal")


def colab_cli_version() -> Optional[str]:
    """已安装 google-colab-cli 的版本（应被 requirements.txt 钉死）。"""
    try:
        from importlib.metadata import version

        return version("google-colab-cli")
    except Exception:
        return None


def probe_kernel_client_api() -> bool:
    """Check the kernel dependency shared by native and CLI execution, offline."""
    try:
        from jupyter_kernel_client import JupyterSubprotocol, KernelClient

        return hasattr(JupyterSubprotocol, "DEFAULT") and all(
            callable(getattr(KernelClient, method, None))
            for method in ("start", "stop", "execute", "execute_interactive")
        )
    except (ImportError, AttributeError):
        return False


def probe_private_api() -> dict:
    """启动自检：native 通道依赖的私有 API 是否仍存在。

    返回 {"contents_client": bool, "kernel_client": bool}；
    任一为 False 时 native 通道不可用。共用内核依赖不兼容时直接报错，
    其他私有接口失效时才降级 cli 并告警。
    """
    probe = {"contents_client": False, "kernel_client": False}
    try:
        from colab_cli.contents import ContentsClient

        probe["contents_client"] = callable(
            getattr(ContentsClient, "_request", None)
        )
    except Exception:
        pass
    try:
        import inspect

        from colab_cli.runtime import ColabRuntime

        # kernel_client 是类上定义的 property；实例化会触发网络连接，不能试。
        probe["kernel_client"] = probe_kernel_client_api() and isinstance(
            inspect.getattr_static(ColabRuntime, "kernel_client", None),
            property,
        )
    except Exception:
        pass
    return probe


def get_transport(
    mode: Optional[str] = None,
    sink: Optional[EventSink] = None,
    backend: Optional[str] = None,
) -> Transport:
    """按计算后端 + 模式构造通道。

    backend: colab（默认，读 COMPUTE_BACKEND）| kaggle | modal。
    colab 后端内 native 探测失败时自动降级 cli 并告警。
    """
    selected_backend = (backend or os.environ.get("COMPUTE_BACKEND") or "colab").lower()
    if selected_backend == "modal":
        from .modal_backend import ModalTransport

        return ModalTransport(sink)
    if selected_backend == "kaggle":
        from .kaggle_backend import KaggleTransport

        return KaggleTransport(sink)
    if selected_backend != "colab":
        raise ValueError(
            f"未知计算后端: {selected_backend!r}（可选: colab | kaggle | modal）"
        )
    selected = (mode or os.environ.get("COLAB_TRANSPORT") or "native").lower()
    if selected not in TRANSPORT_MODES:
        raise ValueError(
            f"未知 COLAB_TRANSPORT: {selected!r}（可选: native | cli）"
        )
    if not probe_kernel_client_api():
        raise TransportError(
            "Colab 内核依赖不兼容：缺少 KernelClient / JupyterSubprotocol 或所需方法。"
            "请在 colab_cli 目录执行 python -m pip install -r requirements.txt，"
            "然后重启 Runner；Docker 部署请重新构建 Runner 镜像。"
        )
    if selected == "cli":
        return CliTransport(sink)

    probe = probe_private_api()
    if all(probe.values()):
        return NativeTransport(sink)

    missing = [name for name, ok in probe.items() if not ok]
    if sink is not None:
        sink(
            "task.start",
            "warn",
            ">>> Colab 私有 API 探测失败（"
            + ", ".join(missing)
            + "），已降级到公共 CLI 通道；任务可继续但无实时下载",
            {
                "reason": "contents_api_probe_failed",
                "fallback": "cli",
                "probe": probe,
            },
        )
    return CliTransport(sink)
