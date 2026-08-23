# =============================================================================
# 本地执行: 把 kernel.py 推送到 Kaggle 云端 GPU 运行, 并轮询状态、下载结果
#
# 前置:
#   1. pip install kaggle, 认证文件放在 ~/.kaggle/ (见项目 README)
#   2. 在 Kaggle 网页创建一个 Dataset, 上传:
#        api.json + 参考图 + 输入视频 (文件名与工作流一致)
#   3. 修改同目录 kernel-metadata.json:
#        - id / title 里的 YOUR_USERNAME 改成你的用户名
#        - dataset_sources 改成上一步的 Dataset (user/slug)
#
# 用法:
#   python push.py            # 推送并等待完成, 完成后下载输出到 ./kernel_output/
#   python push.py --push     # 只推送, 不等待
#   python push.py --status   # 只查询当前状态
#   python push.py --output   # 只下载输出
# =============================================================================
import argparse
import json
import sys
import time
from pathlib import Path

from kaggle.api.kaggle_api_extended import KaggleApi

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "kernel_output"
POLL_INTERVAL = 60  # 秒


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--push", action="store_true", help="只推送不等待")
    parser.add_argument("--status", action="store_true", help="只查询状态")
    parser.add_argument("--output", action="store_true", help="只下载输出")
    args = parser.parse_args()

    meta = json.load(open(HERE / "kernel-metadata.json"))
    ref = meta["id"]
    if "YOUR_USERNAME" in json.dumps(meta):
        sys.exit("ERROR: 请先修改 kernel-metadata.json 里的 YOUR_USERNAME / dataset")

    api = KaggleApi()
    api.authenticate()

    if args.status:
        print(ref, "->", api.kernels_status(ref))
        return
    if args.output:
        download_output(api, ref)
        return

    # 推送 (kernel-metadata.json 所在目录整体作为 kernel 上传)
    print("推送 kernel:", ref)
    api.kernels_push(str(HERE))
    if args.push:
        print("已推送。云端运行约需 1~3 小时(下载模型+采样), "
              "可用 --status 查看进度。")
        return

    # 轮询直到结束
    print("等待云端运行结束 (每 %ds 查询一次)..." % POLL_INTERVAL)
    while True:
        time.sleep(POLL_INTERVAL)
        status = str(api.kernels_status(ref))
        print(time.strftime("%H:%M:%S"), status, flush=True)
        if "complete" in status.lower():
            break
        if "error" in status.lower() or "cancel" in status.lower():
            sys.exit(f"云端运行失败: {status}, "
                     f"到 Kaggle 网页查看 kernel 日志: https://www.kaggle.com/code/{ref}")

    download_output(api, ref)


def download_output(api, ref):
    OUT_DIR.mkdir(exist_ok=True)
    api.kernels_output(ref, str(OUT_DIR))
    print("输出已下载到:", OUT_DIR)
    for f in sorted(OUT_DIR.iterdir()):
        print(" ", f.name, f.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
