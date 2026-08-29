# =============================================================================
# 在 Colab VM 上执行：只负责安装通用 ComfyUI 运行环境。
# 模型准备、输入复制和工作流执行由 remote_workflow.py 负责。
# =============================================================================
import os
import subprocess
import sys

COMFY = "/content/ComfyUI"


def run(cmd):
    """执行安装命令并实时转发输出。"""
    print("+", " ".join(str(part) for part in cmd), flush=True)
    subprocess.run(cmd, check=True)


def main():
    """克隆 ComfyUI 并安装通用运行依赖。"""
    print(">>> 安装 ComfyUI 通用环境", flush=True)
    if not os.path.isdir(COMFY):
        run([
            "git",
            "clone",
            "--depth",
            "1",
            "https://github.com/comfyanonymous/ComfyUI.git",
            COMFY,
        ])
    else:
        print(f"ComfyUI 已存在，跳过克隆: {COMFY}", flush=True)

    run([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        f"{COMFY}/requirements.txt",
    ])
    run([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "huggingface_hub[hf_transfer]",
        "websocket-client",
    ])
    print(">>> ComfyUI 通用环境安装完成", flush=True)


if __name__ == "__main__":
    main()
