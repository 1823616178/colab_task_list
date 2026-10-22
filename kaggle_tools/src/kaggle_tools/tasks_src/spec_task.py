"""按契约文档写实现的评测任务: 模型读 spec 写代码, 用它看不到的验收测试判定。

真评测的关键: 验收测试写在 WORKSPACE 之外, 模型的 read_file 只能解析到 WORKSPACE 内,
所以它拿不到测试源码, 只能从 run_verification 的失败输出里推断 —— 和 SWE-bench 同构。

⚠ 必须单文件自包含: kaggle b t push 只推这一个 .py 并转成 notebook。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import kaggle_benchmarks as kbench
from _workspace_tools import *  # kcloud build 时内联
from kaggle_benchmarks.tools.base import ToolInvocationLimitExhausted
from kaggle_benchmarks.tools.native import native_tool_agent

# ==== 配置区 ================================================================

# 云端在 /kaggle/input 下按这个名字找 spec 文件; 本地用 SPEC_PATH 环境变量。
SPEC_FILE_NAME = "usage-api.md"

TARGET_MODULE = "usage_client.py"

SPEC = f"""\
附带的契约文档描述了一个本地额度聚合服务的 HTTP 接口。
照着它实现一个 Python 客户端模块 {TARGET_MODULE}。

只用标准库(urllib.request / json), 不要引入第三方依赖。

必须提供这些公开名字, 签名严格一致:

    class UsageError(Exception): ...
    class UnknownProviderError(UsageError): ...

    class UsageClient:
        def __init__(self, base_url: str = "http://127.0.0.1:8787",
                     timeout: float = 45.0) -> None
        def get_usage(self, providers: list[str] | None = None) -> dict
        def get_providers(self) -> dict
        def set_providers(self, changes: dict) -> dict
        @staticmethod
        def remaining(used: float) -> float

行为要求:
- get_usage 请求 GET /api/usage; 传了 providers 时拼成逗号分隔的 providers 查询参数
- get_providers 请求 GET /api/providers, 返回里层的映射(不是外层带 providers 键的对象)
- set_providers 用 PUT 请求 /api/providers, body 是 {{"providers": changes}},
  返回更新后的里层映射
- 服务端返回 400 时抛 UnknownProviderError, 其它非 2xx 抛 UsageError,
  两者的异常消息里都要带上服务端给的 error 文本
- remaining 把已用百分比换算成剩余百分比, 并夹在 0 到 100 之间
- base_url 结尾有没有斜杠都要能正常工作

用工具读契约文档和写文件, 随时运行验收命令检查。改到验收通过为止。
"""

# 验收测试: 写在工作区之外, 模型读不到。
ACCEPTANCE_TESTS = '''\
"""按 usage-api.md 契约写的验收测试。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from usage_client import UnknownProviderError, UsageClient, UsageError

RECORDED = []
RESPONSES = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _respond(self):
        status, body = RESPONSES.get(self.path.split("?")[0], (404, {"error": "Not found"}))
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        RECORDED.append(("GET", self.path, None))
        self._respond()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        RECORDED.append(("PUT", self.path, self.rfile.read(length).decode()))
        self._respond()


@pytest.fixture
def server():
    RECORDED.clear()
    RESPONSES.clear()
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", RECORDED, RESPONSES
    httpd.shutdown()


def test_get_usage_calls_the_usage_route(server):
    base, recorded, responses = server
    responses["/api/usage"] = (200, {"displayMode": "remaining", "grok": {"used": 12}})
    result = UsageClient(base).get_usage()
    assert result["grok"]["used"] == 12
    assert recorded[0][0] == "GET"
    assert recorded[0][1].startswith("/api/usage")


def test_get_usage_encodes_providers_as_comma_separated(server):
    base, recorded, responses = server
    responses["/api/usage"] = (200, {})
    UsageClient(base).get_usage(providers=["grok", "claude"])
    assert "providers=grok%2Cclaude" in recorded[0][1] or "providers=grok,claude" in recorded[0][1]


def test_get_usage_without_providers_sends_no_query(server):
    base, recorded, responses = server
    responses["/api/usage"] = (200, {})
    UsageClient(base).get_usage()
    assert "providers" not in recorded[0][1]


def test_bad_request_raises_unknown_provider_error(server):
    base, _, responses = server
    responses["/api/usage"] = (400, {"error": "Unknown tool. Use grok, claude"})
    with pytest.raises(UnknownProviderError) as info:
        UsageClient(base).get_usage(providers=["nope"])
    assert "Unknown tool" in str(info.value)


def test_server_error_raises_usage_error(server):
    base, _, responses = server
    responses["/api/usage"] = (500, {"error": "Internal server error"})
    with pytest.raises(UsageError) as info:
        UsageClient(base).get_usage()
    assert "Internal server error" in str(info.value)


def test_unknown_provider_error_is_a_usage_error():
    assert issubclass(UnknownProviderError, UsageError)


def test_get_providers_returns_the_inner_mapping(server):
    base, _, responses = server
    responses["/api/providers"] = (200, {"providers": {"grok": True, "codex": False}})
    assert UsageClient(base).get_providers() == {"grok": True, "codex": False}


def test_set_providers_puts_a_wrapped_body(server):
    base, recorded, responses = server
    responses["/api/providers"] = (200, {"providers": {"grok": True, "codex": False}})
    result = UsageClient(base).set_providers({"codex": False})
    assert recorded[0][0] == "PUT"
    assert json.loads(recorded[0][2]) == {"providers": {"codex": False}}
    assert result == {"grok": True, "codex": False}


def test_remaining_inverts_the_used_percentage():
    assert UsageClient.remaining(0) == 100
    assert UsageClient.remaining(30) == 70
    assert UsageClient.remaining(99.5) == pytest.approx(0.5)


def test_remaining_clamps_out_of_range_values():
    assert UsageClient.remaining(120) == 0
    assert UsageClient.remaining(-5) == 100


def test_default_timeout_is_at_least_45_seconds():
    assert UsageClient("http://127.0.0.1:1").timeout >= 45


def test_trailing_slash_in_base_url_is_tolerated(server):
    base, recorded, responses = server
    responses["/api/usage"] = (200, {})
    UsageClient(base + "/").get_usage()
    assert recorded[0][1].split("?")[0] == "/api/usage"
'''

VERIFY_OUTPUT_TAIL = 5_000
MAX_TOOL_ROUNDS = 20

# 验收测试放在工作区之外, resolve_inside_workspace 保证模型读不到。
ACCEPTANCE_DIRECTORY = OUTPUT_DIRECTORY / "_acceptance"


def locate_spec() -> Path:
    """定位契约文档: 优先环境变量, 其次 /kaggle/input 递归查找。"""
    override = os.environ.get("SPEC_PATH")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"SPEC_PATH 指向的文件不存在: {path}")
        return path

    for root in (KAGGLE_INPUT, Path.cwd()):
        if root.is_dir():
            for candidate in sorted(root.rglob(SPEC_FILE_NAME)):
                if candidate.is_file():
                    return candidate
    raise FileNotFoundError(
        f"找不到 {SPEC_FILE_NAME}。云端请确认 push 时用 -d 挂载了数据集, "
        f"本地请设置 SPEC_PATH。"
    )


def prepare_directories() -> None:
    """建工作区, 并把验收测试写到工作区之外。"""
    for directory in (WORKSPACE, ACCEPTANCE_DIRECTORY):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
    (ACCEPTANCE_DIRECTORY / "test_usage_client.py").write_text(ACCEPTANCE_TESTS, encoding="utf-8")


def execute_verification() -> subprocess.CompletedProcess:
    """用工作区外的验收测试跑 pytest, 把工作区放进 PYTHONPATH。"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(WORKSPACE)
    return run_command(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(ACCEPTANCE_DIRECTORY)],
        cwd=WORKSPACE,
        env=environment,
    )


# ==== 工具 ==================================================================


def read_spec() -> str:
    """读取契约文档全文, 里面写清了接口的路由、参数和错误响应。"""
    return locate_spec().read_text(encoding="utf-8")[:MAX_FILE_BYTES]


TOOLS = [read_spec, list_files, read_file, write_file, run_verification]


# ==== 任务 ==================================================================


def write_artifacts(result: subprocess.CompletedProcess) -> None:
    """把生成的代码和验收报告留在输出目录, 随 run 产物打包。"""
    (OUTPUT_DIRECTORY / "verification.txt").write_text(
        f"exit_code={result.returncode}\n\n{result.stdout}\n{result.stderr}",
        encoding="utf-8",
    )
    for path in sorted(WORKSPACE.rglob("*")):
        if is_tracked(path):
            destination = OUTPUT_DIRECTORY / "generated" / path.relative_to(WORKSPACE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


@kbench.task(
    name="spec-codegen",
    description="让模型照 API 契约文档实现客户端, 用它看不到的验收测试判定",
)
def spec_codegen(llm) -> bool:
    """模型读 spec 写实现, 断言取验收测试的真实退出码。"""
    spec_path = locate_spec()
    print(f"契约文档: {spec_path} ({spec_path.stat().st_size} B)", flush=True)
    prepare_directories()
    print(f"工作区: {WORKSPACE}", flush=True)
    print(f"验收测试: {ACCEPTANCE_DIRECTORY} (模型不可读)", flush=True)

    kbench.user.send(SPEC)
    try:
        native_tool_agent(llm, TOOLS, max_tool_rounds=MAX_TOOL_ROUNDS)
    except ToolInvocationLimitExhausted:
        print(f"⚠ 工具调用轮数用尽({MAX_TOOL_ROUNDS} 轮), 直接进入最终验收。", flush=True)

    result = execute_verification()
    write_artifacts(result)

    generated = WORKSPACE / TARGET_MODULE
    print("=== 生成的代码 ===", flush=True)
    print(generated.read_text(encoding="utf-8") if generated.is_file() else "(没有生成目标文件)", flush=True)
    print("=== 最终验收 ===", flush=True)
    print(f"exit_code={result.returncode}", flush=True)
    print(result.stdout[-2500:], flush=True)

    kbench.assertions.assert_equal(
        0, result.returncode,
        expectation=f"生成的 {TARGET_MODULE} 要通过全部 12 个契约验收测试",
    )
    return result.returncode == 0


if not import_only():
    spec_codegen.run(kbench.llm)  # type: ignore[attr-defined]
