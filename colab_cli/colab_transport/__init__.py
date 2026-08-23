"""colab_transport：Colab 通道适配层。

用法：

    from colab_transport import get_transport

    transport = get_transport()          # 读 COLAB_TRANSPORT，默认 native
    transport = get_transport("cli")     # 强制公共 CLI 通道

私有 API 断裂是高危风险（doc/01 §5 R1），native 模式启用前经
`probe_private_api()` 自检，失败自动降级 cli 并经 sink 发出 warn 事件。
"""
import os
from typing import Optional

from .base import EventSink, Transport, TransportError
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
    "colab_cli_version",
    "get_transport",
    "probe_private_api",
    "resolve_config_path",
]

TRANSPORT_MODES = ("native", "cli")


def colab_cli_version() -> Optional[str]:
    """已安装 google-colab-cli 的版本（应被 requirements.txt 钉死）。"""
    try:
        from importlib.metadata import version

        return version("google-colab-cli")
    except Exception:
        return None


def probe_private_api() -> dict:
    """启动自检：native 通道依赖的私有 API 是否仍存在。

    返回 {"contents_client": bool, "kernel_client": bool}；
    任一为 False 时 native 通道不可用，必须降级 cli 并告警。
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
        probe["kernel_client"] = isinstance(
            inspect.getattr_static(ColabRuntime, "kernel_client", None),
            property,
        )
    except Exception:
        pass
    return probe


def get_transport(
    mode: Optional[str] = None,
    sink: Optional[EventSink] = None,
) -> Transport:
    """按模式构造通道；native 探测失败时自动降级 cli 并告警。"""
    selected = (mode or os.environ.get("COLAB_TRANSPORT") or "native").lower()
    if selected not in TRANSPORT_MODES:
        raise ValueError(
            f"未知 COLAB_TRANSPORT: {selected!r}（可选: native | cli）"
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
