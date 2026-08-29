"""`python3 -m colab_runner` 启动入口：uvicorn 单 worker，只绑 127.0.0.1。

环境变量：
- `COLAB_RUNNER_TOKEN`（必填）：X-Internal-Token 共享密钥；
- `COLAB_RUNNER_PORT`（默认 8765）；
- `COLAB_RUNNER_WORKERS`（默认 4，任务线程数；并发上限实际是 Colab VM 数）；
- `COLAB_OUTPUT_DIR` / `COLAB_TRANSPORT` / `COLAB_CLI_STATE_DIR`：见 P0 文档。
"""
import os

import uvicorn

from .app import create_app


def main() -> None:
    token = os.environ.get("COLAB_RUNNER_TOKEN")
    if not token:
        raise SystemExit(
            "错误: 缺少环境变量 COLAB_RUNNER_TOKEN（X-Internal-Token 共享密钥）"
        )
    port = int(os.environ.get("COLAB_RUNNER_PORT", "8765"))
    # doc/01 §7：只监听 127.0.0.1，绝不暴露公网 —— 它能凭 ADC 创建 A100 VM
    uvicorn.run(
        create_app(token=token),
        host="127.0.0.1",
        port=port,
        workers=1,  # doc/05 P1：单 worker（session 互斥锁是进程内的）
    )


if __name__ == "__main__":
    main()
