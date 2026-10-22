"""kllm 命令行入口: serve / doctor / models / ask / usage。"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from .. import __version__
from ..common.config import AVAILABLE_MODELS, Settings, load_settings
from ..common.errors import KaggleToolsError
from ..common.logging import get_logger, setup_logging
from ..common.retry import RetryPolicy, is_retryable_status
from .credentials import TokenManager
from .usage import append_record, extract_record, format_report

logger = get_logger(__name__)


def command_serve(args: argparse.Namespace, settings: Settings) -> int:
    """启动本地网关。"""
    import uvicorn

    from .server import create_app

    host = args.host or settings.host
    port = args.port or settings.port
    print(f"kllm 网关: http://{host}:{port}/v1")
    if settings.local_key is None:
        print("未设置 KLLM_LOCAL_KEY, 本机任意进程都可调用(仅监听回环地址)。")
    uvicorn.run(create_app(settings), host=host, port=port,
                log_level=settings.log_level.lower())
    return 0


def command_doctor(args: argparse.Namespace, settings: Settings) -> int:
    """自检: 凭据能否获取, 代理能否调通。"""
    manager = TokenManager(settings)
    print("[1] 获取模型代理凭据 ...")
    credentials = manager.get()
    print(f"    base_url = {credentials.base_url}")
    print(f"    token    = ...{credentials.token[-4:]}  过期于 {credentials.expiry}")

    model = args.model or settings.default_model
    print(f"\n[2] 试调 {model} ...")
    response = chat_once(credentials, model, "Reply with exactly: OK", settings)
    print(f"    HTTP {response.status_code}")
    if response.status_code != 200:
        print(f"    响应: {response.text[:600]}")
        if "location" in response.text and "not supported" in response.text:
            print("    诊断: 账号被判定为不受支持地区, 代理侧拒绝, 换出口 IP 或联系 Kaggle。")
        return 1
    body = response.json()
    print(f"    内容: {body['choices'][0]['message']['content'][:100]!r}")
    print(f"    用量: {json.dumps(body.get('usage', {}), ensure_ascii=False)}")
    print("\n自检通过。")
    return 0


def command_models(args: argparse.Namespace, settings: Settings) -> int:
    """列出可用模型。"""
    for model in AVAILABLE_MODELS:
        marker = " (默认)" if model == settings.default_model else ""
        print(f"{model}{marker}")
    return 0


def command_ask(args: argparse.Namespace, settings: Settings) -> int:
    """不起服务, 直接发一次提问(用于快速验证)。"""
    credentials = TokenManager(settings).get()
    model = args.model or settings.default_model
    response = chat_once(credentials, model, args.prompt, settings)
    if response.status_code != 200:
        print(f"HTTP {response.status_code}: {response.text[:600]}", file=sys.stderr)
        return 1
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        print(f"上游返回了无法解析的内容: {response.text[:600]}", file=sys.stderr)
        return 1
    print(content)
    append_record(settings.usage_path, extract_record(model, body, streamed=False))
    return 0


def command_usage(args: argparse.Namespace, settings: Settings) -> int:
    """打印用量账本汇总。"""
    print(format_report(settings.usage_path))
    return 0


def chat_once(credentials, model: str, prompt: str, settings: Settings,
              transport: httpx.BaseTransport | None = None,
              sleep=time.sleep) -> httpx.Response:
    """向代理发一次非流式 chat/completions; 429/5xx/网络错误按策略重试。"""
    policy = RetryPolicy(settings.max_retries, settings.retry_base_delay, settings.retry_max_delay)
    with httpx.Client(timeout=settings.request_timeout_seconds, transport=transport) as client:
        attempt = 0
        while True:
            try:
                response = client.post(
                    f"{credentials.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {credentials.token}",
                        "Content-Type": "application/json",
                    },
                    json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                )
            except httpx.TransportError as error:
                if attempt >= policy.max_retries:
                    raise KaggleToolsError(f"无法连接模型代理: {error}") from error
                delay = policy.delay(attempt)
                logger.warning("网络错误 %s, %.1fs 后重试", error, delay)
            else:
                if not is_retryable_status(response.status_code) or attempt >= policy.max_retries:
                    return response
                delay = policy.delay(attempt, response.headers.get("retry-after"))
                logger.warning("HTTP %s, %.1fs 后重试", response.status_code, delay)
            attempt += 1
            sleep(delay)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="kllm",
        description="把 Kaggle AI 额度包装成本地 OpenAI 兼容模型后端",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="启动本地网关")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.set_defaults(handler=command_serve)

    doctor_parser = subparsers.add_parser("doctor", help="自检凭据与代理连通性")
    doctor_parser.add_argument("-m", "--model", default=None)
    doctor_parser.set_defaults(handler=command_doctor)

    models_parser = subparsers.add_parser("models", help="列出可用模型")
    models_parser.set_defaults(handler=command_models)

    ask_parser = subparsers.add_parser("ask", help="直接发一次提问")
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("-m", "--model", default=None)
    ask_parser.set_defaults(handler=command_ask)

    usage_parser = subparsers.add_parser("usage", help="查看用量与花费估算")
    usage_parser.set_defaults(handler=command_usage)

    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行主入口: 可预期的错误打印一行提示并返回 1, 不打调用栈。"""
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        setup_logging("DEBUG" if args.verbose else settings.log_level, settings.log_file)
        return args.handler(args, settings)
    except KaggleToolsError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
