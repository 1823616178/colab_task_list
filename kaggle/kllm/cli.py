"""kllm 命令行入口: serve / doctor / models / ask / usage。"""

import argparse
import json
import sys

import httpx

from .config import AVAILABLE_MODELS, DEFAULT_MODEL, load_settings
from .credentials import CredentialError, TokenManager
from .usage import append_record, extract_record, format_report


def command_serve(args: argparse.Namespace) -> int:
    """启动本地网关。"""
    import uvicorn

    from .server import create_app

    settings = load_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    print(f"kllm 网关: http://{host}:{port}/v1")
    if settings.local_key is None:
        print("未设置 KLLM_LOCAL_KEY, 本机任意进程都可调用(仅监听回环地址)。")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    """自检: 凭据能否获取, 代理能否调通。"""
    settings = load_settings()
    manager = TokenManager(settings)
    print("[1] 获取模型代理凭据 ...")
    try:
        credentials = manager.get()
    except CredentialError as error:
        print(f"    失败: {error}")
        return 1
    print(f"    base_url = {credentials.base_url}")
    print(f"    token    = ...{credentials.token[-4:]}  过期于 {credentials.expiry}")

    print(f"\n[2] 试调 {args.model} ...")
    response = _chat(credentials, args.model, "Reply with exactly: OK")
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


def command_models(args: argparse.Namespace) -> int:
    """列出可用模型。"""
    for model in AVAILABLE_MODELS:
        marker = " (默认)" if model == DEFAULT_MODEL else ""
        print(f"{model}{marker}")
    return 0


def command_ask(args: argparse.Namespace) -> int:
    """不起服务, 直接发一次提问(用于快速验证)。"""
    settings = load_settings()
    manager = TokenManager(settings)
    try:
        credentials = manager.get()
    except CredentialError as error:
        print(f"获取凭据失败: {error}", file=sys.stderr)
        return 1
    response = _chat(credentials, args.model, args.prompt)
    if response.status_code != 200:
        print(f"HTTP {response.status_code}: {response.text[:600]}", file=sys.stderr)
        return 1
    body = response.json()
    print(body["choices"][0]["message"]["content"])
    append_record(settings.usage_path, extract_record(args.model, body, streamed=False))
    return 0


def command_usage(args: argparse.Namespace) -> int:
    """打印用量账本汇总。"""
    print(format_report(load_settings().usage_path))
    return 0


def _chat(credentials, model: str, prompt: str) -> httpx.Response:
    """向代理发一次非流式 chat/completions。"""
    return httpx.post(
        f"{credentials.base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=300.0,
    )


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="kllm",
        description="把 Kaggle AI 额度包装成本地 OpenAI 兼容模型后端",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="启动本地网关")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.set_defaults(handler=command_serve)

    doctor_parser = subparsers.add_parser("doctor", help="自检凭据与代理连通性")
    doctor_parser.add_argument("-m", "--model", default=DEFAULT_MODEL)
    doctor_parser.set_defaults(handler=command_doctor)

    models_parser = subparsers.add_parser("models", help="列出可用模型")
    models_parser.set_defaults(handler=command_models)

    ask_parser = subparsers.add_parser("ask", help="直接发一次提问")
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("-m", "--model", default=DEFAULT_MODEL)
    ask_parser.set_defaults(handler=command_ask)

    usage_parser = subparsers.add_parser("usage", help="查看用量与花费估算")
    usage_parser.set_defaults(handler=command_usage)

    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行主入口。"""
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
