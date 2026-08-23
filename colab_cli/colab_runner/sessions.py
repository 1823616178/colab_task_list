"""GET /sessions 的 colab CLI 包装（doc/02 §2.4）。

数据来自三处，全部失败容忍（Runner 不能因为 colab 侧问题 500）：

1. **本地状态文件**（只读 JSON）：会话名 → endpoint/variant 映射。
   支持 `COLAB_CLI_STATE_DIR`（每会话一文件，Runner 生产形态）、
   `COLAB_CONFIG`（单文件）与 CLI 默认路径三种布局；
2. **`colab status -s <name>`**：逐会话拿 IDLE/BUSY；
3. **`colab sessions`**：服务器端真相（list_assignments），提供 reachable
   与未映射会话（名称为 "?" 的孤儿 assignment，doc/01 §5 R2 的检测对象）。
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

from colab_transport.cli import ColabCli

# _format_session_line：``[name] endpoint | Hardware: X | Variant: Y[ | Status: Z]``
_SESSION_LINE = re.compile(
    r"^\[(?P<name>[^\]]*)\]\s+(?P<endpoint>\S+)"
    r"\s*\|\s*Hardware:\s*(?P<hardware>[^|]+?)"
    r"\s*\|\s*Variant:\s*(?P<variant>[^|]+?)"
    r"(?:\s*\|\s*Status:\s*(?P<status>.*))?\s*$"
)

_ACCELERATOR_MAP = {"GPU": "gpu", "TPU": "tpu", "CPU": "cpu"}


def parse_session_line(line: str) -> Optional[dict]:
    """解析一行 `[name] endpoint | Hardware: X | Variant: Y[ | Status: Z]`。"""
    match = _SESSION_LINE.match(line.strip())
    if not match:
        return None
    status_text = (match.group("status") or "").strip()
    if status_text.startswith("BUSY"):
        status = "BUSY"
    elif status_text.startswith("IDLE"):
        status = "IDLE"
    else:
        status = "UNKNOWN"
    hardware = match.group("hardware").strip().upper()
    return {
        "name": match.group("name"),
        "endpoint": match.group("endpoint"),
        "variant": match.group("variant").strip(),
        "accelerator": _ACCELERATOR_MAP.get(hardware, hardware.lower() or "cpu"),
        "status": status,
    }


def _state_files() -> list[Path]:
    """按环境变量布局枚举本地 CLI 状态文件（只读，不写）。"""
    state_dir = os.environ.get("COLAB_CLI_STATE_DIR")
    if state_dir:
        directory = Path(state_dir).expanduser()
        if directory.is_dir():
            return sorted(directory.glob("*.json"))
        return []
    single = os.environ.get("COLAB_CONFIG")
    if single:
        path = Path(single).expanduser()
        return [path] if path.is_file() else []
    default = Path.home() / ".config" / "colab-cli" / "sessions.json"
    return [default] if default.is_file() else []


def _local_sessions() -> dict[str, dict]:
    """从本地状态文件读 name -> {endpoint, variant}；文件损坏则跳过。"""
    local: dict[str, dict] = {}
    for path in _state_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for name, entry in data.items():
            if isinstance(entry, dict):
                local[name] = {
                    "endpoint": entry.get("endpoint", ""),
                    "variant": entry.get("variant", ""),
                }
    return local


def _capture(cli: ColabCli, *args) -> Optional[str]:
    """运行 colab 子命令并捕获输出；任何失败返回 None（降级不致命）。"""
    try:
        result = cli.run_capture(*args, echo=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def list_sessions() -> list[dict]:
    """聚合本地状态 + colab status + colab sessions，返回 doc/02 §2.4 的列表。"""
    merged: dict[str, dict] = {}  # key: endpoint（优先）或 name

    def put(entry: dict) -> dict:
        key = entry.get("endpoint") or entry.get("name")
        if key not in merged:
            merged[key] = {
                "name": entry.get("name") or "?",
                "status": "UNKNOWN",
                "variant": None,
                "accelerator": None,
                "endpoint": entry.get("endpoint") or "",
                "reachable": False,
            }
        target = merged[key]
        for field_name in ("name", "status", "variant", "accelerator", "endpoint"):
            value = entry.get(field_name)
            if value not in (None, "", "?", "UNKNOWN"):
                target[field_name] = value
        if entry.get("reachable"):
            target["reachable"] = True
        return target

    # 1) 本地状态文件：已知会话名
    local = _local_sessions()

    # 2) colab status -s <name>：逐会话 IDLE/BUSY
    for name, info in local.items():
        output = _capture(ColabCli(name), "status", "-s", name)
        parsed = None
        for line in (output or "").splitlines():
            parsed = parse_session_line(line)
            if parsed is not None:
                break
        if parsed is None:
            put({"name": name, "endpoint": info.get("endpoint", ""),
                 "variant": info.get("variant") or None})
        else:
            put(parsed)

    # 3) colab sessions：服务器真相，标 reachable；未映射的给 "?" 名
    # （ColabCli(None) 走 COLAB_CLI_STATE_DIR/COLAB_CONFIG/默认三级解析；
    # 服务器端 assignment 列表与本地状态文件无关，auth 失败时整体降级为空）
    output = _capture(ColabCli(None), "sessions")
    for line in (output or "").splitlines():
        parsed = parse_session_line(line)
        if parsed is None:
            continue
        parsed["reachable"] = True
        if parsed["name"] == "?":
            # 服务器端有、本地无映射 —— 尝试按 endpoint 找回名字
            for name, info in local.items():
                if info.get("endpoint") == parsed["endpoint"]:
                    parsed["name"] = name
                    break
        put(parsed)

    return sorted(merged.values(), key=lambda item: item["name"])
