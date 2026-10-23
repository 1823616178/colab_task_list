"""GET /sessions 的 colab CLI 包装（doc/02 §2.4）。

云端查询失败返回明确错误，不能把未知状态当作 VM 消失。

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
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
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
SNAPSHOT_TTL_SECONDS = 5.0
QUERY_TIMEOUT_SECONDS = 5.0
STATUS_BUDGET_SECONDS = 3.0
_cache_lock = threading.Lock()
_cache: tuple[float, list[dict]] = (0.0, [])
_generation = 0
_inflight: Optional[Future] = None


class SessionQueryError(RuntimeError):
    """没有取得权威云端会话列表，调用方必须保留上次已知状态。"""


def invalidate_sessions() -> None:
    global _cache, _generation
    with _cache_lock:
        _cache = (0.0, [])
        _generation += 1


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
    variant = match.group("variant").strip()
    # CLI 0.6 的 Hardware 为 L4/T4，Variant 为 GPU；兼容旧格式。
    if hardware not in _ACCELERATOR_MAP and variant.upper() in _ACCELERATOR_MAP:
        hardware, variant = variant.upper(), match.group("hardware").strip()
    return {
        "name": match.group("name"),
        "endpoint": match.group("endpoint"),
        "variant": variant,
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


def _capture(cli: ColabCli, *args, timeout=QUERY_TIMEOUT_SECONDS) -> Optional[str]:
    """运行 colab 子命令并捕获输出；任何失败返回 None（降级不致命）。"""
    try:
        result = subprocess.run(
            cli.build(*args), capture_output=True, text=True,
            check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def list_sessions() -> list[dict]:
    """短期成功缓存 + 并发请求合并；失败不缓存，返回副本供 API 添加 task_id。"""
    global _cache, _inflight
    with _cache_lock:
        if time.monotonic() < _cache[0]:
            return deepcopy(_cache[1])
        leader = _inflight is None
        if leader:
            _inflight = Future()
        future = _inflight
        generation = _generation
    if not leader:
        return deepcopy(future.result())
    try:
        entries = _query_sessions()
        with _cache_lock:
            if generation == _generation:
                _cache = (time.monotonic() + SNAPSHOT_TTL_SECONDS, entries)
        future.set_result(entries)
        return deepcopy(entries)
    except Exception as error:
        future.set_exception(error)
        raise
    finally:
        with _cache_lock:
            _inflight = None


def _query_sessions() -> list[dict]:
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

    # 先取得云端权威列表。空输出/无法解析不能作为“没有 VM”的证据。
    output = _capture(ColabCli(None), "sessions")
    if output is None:
        raise SessionQueryError("无法查询 Colab 云端会话（网络、认证或超时），请稍后重试")
    cloud = []
    for line in output.splitlines():
        parsed = parse_session_line(line)
        if parsed is not None:
            cloud.append(parsed)
        elif "Hardware:" in line or "Variant:" in line:
            raise SessionQueryError("Colab 会话列表部分条目无法解析，保留原有会话状态")
    if not cloud and "No active sessions found on server." not in output:
        raise SessionQueryError("Colab 云端会话列表无法解析，保留原有会话状态")

    for name, info in local.items():
        put({"name": name, "endpoint": info.get("endpoint", ""),
             "variant": info.get("variant") or None})

    # 3) colab sessions：服务器真相，标 reachable；未映射的给 "?" 名
    # （ColabCli(None) 走 COLAB_CLI_STATE_DIR/COLAB_CONFIG/默认三级解析；
    # 服务器端 assignment 列表与本地状态文件无关；认证失败由上面的边界报错）
    for parsed in cloud:
        parsed["reachable"] = True
        if parsed["name"] == "?":
            # 服务器端有、本地无映射 —— 尝试按 endpoint 找回名字
            for name, info in local.items():
                if info.get("endpoint") == parsed["endpoint"]:
                    parsed["name"] = name
                    break
        put(parsed)

    # 只探测云端确认存活且状态未知的会话；历史本地配置不再触发子进程。
    deadline = time.monotonic() + STATUS_BUDGET_SECONDS

    def probe(entry):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        output = _capture(ColabCli(entry["name"]), "status", "-s", entry["name"],
                          timeout=remaining)
        for line in (output or "").splitlines():
            parsed = parse_session_line(line)
            if parsed and parsed["endpoint"] == entry["endpoint"]:
                return parsed
        return None

    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="session-status")
    try:
        futures = [executor.submit(probe, entry) for entry in merged.values()
                   if entry["reachable"] and entry["status"] == "UNKNOWN"
                   and entry["name"] != "?"]
        done, _ = wait(futures, timeout=STATUS_BUDGET_SECONDS)
        for future in done:
            parsed = future.result()
            if parsed:
                put(parsed)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return sorted(merged.values(), key=lambda item: item["name"])
