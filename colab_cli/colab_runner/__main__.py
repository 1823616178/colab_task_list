"""`python3 -m colab_runner` 启动入口：uvicorn 单 worker，默认只绑 127.0.0.1。

环境变量：
- `COLAB_RUNNER_TOKEN`（必填）：X-Internal-Token 共享密钥；
- `COLAB_RUNNER_PORT`（默认 8765）；
- `COLAB_RUNNER_HOST`（默认 127.0.0.1）：容器内需绑 0.0.0.0 时设置，且必须同时设
  `COLAB_RUNNER_ALLOW_NON_LOOPBACK=1` 明确确认（只应在 docker 内部网络、不映射宿主端口时使用）；
- `COLAB_RUNNER_WORKERS`（默认 4，任务线程数；并发上限实际是 Colab VM 数）；
- `COLAB_OUTPUT_DIR` / `COLAB_TRANSPORT` / `COLAB_CLI_STATE_DIR`：见 P0 文档。
"""
import os

import uvicorn

from .app import create_app


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def resolve_host(environ) -> str:
    """监听地址：默认 127.0.0.1；非回环地址必须显式确认 COLAB_RUNNER_ALLOW_NON_LOOPBACK=1。"""
    host = (environ.get("COLAB_RUNNER_HOST") or "127.0.0.1").strip()
    if host not in LOOPBACK_HOSTS and environ.get("COLAB_RUNNER_ALLOW_NON_LOOPBACK") != "1":
        raise SystemExit(
            f"错误: COLAB_RUNNER_HOST={host} 不是回环地址。Runner 能创建计费 VM，"
            "只应在 docker 内部网络中这样做；确认后再设 COLAB_RUNNER_ALLOW_NON_LOOPBACK=1"
        )
    return host


def main() -> None:
    token = os.environ.get("COLAB_RUNNER_TOKEN")
    if not token:
        raise SystemExit(
            "错误: 缺少环境变量 COLAB_RUNNER_TOKEN（X-Internal-Token 共享密钥）"
        )
    port = int(os.environ.get("COLAB_RUNNER_PORT", "8765"))
    host = resolve_host(os.environ)
    # doc/01 §7：默认只监听 127.0.0.1，绝不暴露公网 —— 它能凭 ADC 创建 A100 VM
    uvicorn.run(
        create_app(token=token),
        host=host,
        port=port,
        workers=1,  # doc/05 P1：单 worker（session 互斥锁是进程内的）
    )


if __name__ == "__main__":
    main()
