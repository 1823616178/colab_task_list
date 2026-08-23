# =============================================================================
# Kaggle 云端执行的 Kernel 脚本: 部署 ComfyUI 并运行 Wan2.2 Animate 工作流
#
# 本脚本由 kaggle kernels push 推送到云端 GPU Notebook 执行。
# 需要挂载一个 Dataset (kernel-metadata.json 的 dataset_sources), 里面放:
#   - api.json            (ComfyUI 工作流)
#   - 78515578_109_5e22.jpg  (参考图, 文件名必须与工作流一致)
#   - *.mp4               (输入视频, 文件名必须与工作流一致)
#
# 生成的视频会写到 /kaggle/working, 运行结束后可用
#   kaggle kernels output <kernel> 下载。
# =============================================================================
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

BASE_DIR = "/kaggle/tmp"        # 临时盘: 装 ComfyUI 和模型, 不占输出配额
WORK_DIR = "/kaggle/working"    # 输出盘: 只有这里的内容会作为 kernel 结果保存
COMFY = os.path.join(BASE_DIR, "ComfyUI")
PORT = 8188

CUSTOM_NODES = [
    "https://github.com/ltdrdata/ComfyUI-Manager",               # 管理器(可选, 方便排查)
    "https://github.com/rgthree/rgthree-comfy",                  # Any Switch / Power Lora Loader
    "https://github.com/ltdrdata/ComfyUI-Impact-Pack",           # BboxDetectorSEGS / SegsToCombinedMask ...
    "https://github.com/ltdrdata/ComfyUI-Impact-Subpack",        # UltralyticsDetectorProvider
    "https://github.com/Fannovel16/comfyui_controlnet_aux",      # DWPreprocessor
    "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite",   # VHS_LoadVideo / VHS_VideoCombine
    "https://github.com/kijai/ComfyUI-KJNodes",                  # ImageResizeKJv2 / PathchSageAttentionKJ / BlockifyMask / DrawMaskOnImage
    "https://github.com/city96/ComfyUI-GGUF",                    # UnetLoaderGGUF
    "https://github.com/pythongosssss/ComfyUI-Custom-Scripts",   # MathExpression|pysssss
    "https://github.com/yolain/ComfyUI-Easy-Use",                # easy indexAnything
    "https://github.com/chflame163/ComfyUI_LayerStyle",          # LayerUtility: PurgeVRAM
    "https://github.com/chflame163/ComfyUI_LayerStyle_Advance",  # LayerUtility: PurgeVRAM V2
    "https://github.com/1038lab/ComfyUI-RMBG",                   # ClothesSegment
    "https://github.com/jamesWalker55/comfyui-various",          # JWImageResizeByShorterSide
    "https://github.com/melMass/comfy_mtb",                      # PixelPerfectResolution
]

# (HF仓库, 仓库内路径, 存放目录)
MODELS = [
    ("QuantStack/Wan2.2-Animate-14B-GGUF", "Wan2.2-Animate-14B-Q2_K.gguf",
     f"{COMFY}/models/diffusion_models"),
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/text_encoders/umt5_xxl_fp16.safetensors",
     f"{COMFY}/models/text_encoders"),
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/vae/wan_2.1_vae.safetensors",
     f"{COMFY}/models/vae"),
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/clip_vision/clip_vision_h.safetensors",
     f"{COMFY}/models/clip_vision"),
    ("Bingsu/adetailer", "face_yolov8m.pt",
     f"{COMFY}/models/ultralytics/bbox"),
    ("hr16/yolox-onnx", "yolox_l.torchscript.pt",
     f"{COMFY}/custom_nodes/comfyui_controlnet_aux/ckpts/hr16/yolox-onnx"),
    ("hr16/DWPose-TorchScript-BatchSize5", "dw-ll_ucoco_384_bs5.torchscript.pt",
     f"{COMFY}/custom_nodes/comfyui_controlnet_aux/ckpts/hr16/DWPose-TorchScript-BatchSize5"),
]


def run(cmd, **kwargs):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def pip_install(*args):
    run([sys.executable, "-m", "pip", "install", "-q", *args])


def main():
    # ---------- 1. 找工作流文件 ----------
    print(">>> [1/6] 查找 api.json", flush=True)
    hits = sorted(glob.glob("/kaggle/input/**/api.json", recursive=True))
    if not hits:
        raise SystemExit("ERROR: 挂载的 Dataset 里没有 api.json")
    api_json = hits[0]
    print("使用工作流:", api_json, flush=True)

    # ---------- 2. 安装 ComfyUI ----------
    print(">>> [2/6] 安装 ComfyUI", flush=True)
    if not os.path.isdir(COMFY):
        run(["git", "clone", "--depth", "1",
             "https://github.com/comfyanonymous/ComfyUI.git", COMFY])
    pip_install("-r", os.path.join(COMFY, "requirements.txt"))
    pip_install("websocket-client", "huggingface_hub[hf_transfer]")

    # ---------- 3. 自定义节点 ----------
    print(">>> [3/6] 安装自定义节点", flush=True)
    nodes_dir = os.path.join(COMFY, "custom_nodes")
    os.makedirs(nodes_dir, exist_ok=True)
    for repo in CUSTOM_NODES:
        name = os.path.basename(repo)
        dest = os.path.join(nodes_dir, name)
        if not os.path.isdir(dest):
            subprocess.run(["git", "clone", "--depth", "1", repo, dest])
    for req in glob.glob(os.path.join(nodes_dir, "*", "requirements.txt")):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req])

    # ---------- 4. 下载模型 ----------
    print(">>> [4/6] 下载模型 (约 15GB)", flush=True)
    from huggingface_hub import hf_hub_download
    for repo, filename, dest_dir in MODELS:
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, os.path.basename(filename))
        if os.path.exists(target):
            print("已存在, 跳过:", filename, flush=True)
            continue
        print(f"下载 {repo}/{filename}", flush=True)
        shutil.copy(hf_hub_download(repo, filename), target)

    # ---------- 5. 拷贝输入素材 ----------
    print(">>> [5/6] 拷贝输入素材", flush=True)
    input_dir = os.path.join(COMFY, "input")
    os.makedirs(input_dir, exist_ok=True)
    media = []
    for ext in ("mp4", "mov", "webm", "jpg", "jpeg", "png", "webp"):
        media += glob.glob(f"/kaggle/input/**/*.{ext}", recursive=True)
        media += glob.glob(f"/kaggle/input/**/*.{ext.upper()}", recursive=True)
    for f in media:
        shutil.copy(f, input_dir)
    print("input 目录:", os.listdir(input_dir), flush=True)

    # ---------- 6. 启动服务并运行工作流 ----------
    print(">>> [6/6] 启动 ComfyUI 并运行工作流", flush=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    log = open(os.path.join(WORK_DIR, "comfyui.log"), "w")
    # T4 16GB 显存较紧, 如 OOM 可在参数里追加 "--lowvram"
    server = subprocess.Popen(
        [sys.executable, "main.py", "--port", str(PORT), "--disable-auto-launch"],
        cwd=COMFY, stdout=log, stderr=subprocess.STDOUT)

    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/system_stats", timeout=2)
            break
        except Exception:
            time.sleep(5)
    else:
        raise SystemExit("ERROR: ComfyUI 启动超时, 日志见 comfyui.log")
    print("ComfyUI 已启动", flush=True)

    try:
        run_workflow(api_json)
    finally:
        server.terminate()
        log.close()
    print(">>> 完成! 输出在 /kaggle/working", flush=True)


def run_workflow(api_json):
    wf = json.load(open(api_json))

    def post(path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req))

    prompt_id = post("/prompt", {"prompt": wf})["prompt_id"]
    print("prompt_id:", prompt_id, flush=True)

    while True:
        time.sleep(10)
        history = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/history/{prompt_id}"))
        if prompt_id not in history:
            print("排队/执行中...", flush=True)
            continue
        entry = history[prompt_id]
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            print("执行出错:", json.dumps(status.get("messages", []),
                                        ensure_ascii=False), flush=True)
            raise SystemExit(1)
        if status.get("completed"):
            break

    print("执行完成, 收集输出文件", flush=True)
    for node_out in entry.get("outputs", {}).values():
        for key in ("images", "gifs", "videos"):
            for item in node_out.get(key, []):
                url = (f"http://127.0.0.1:{PORT}/view?filename={item['filename']}"
                       f"&subfolder={item.get('subfolder', '')}"
                       f"&type={item.get('type', 'output')}")
                data = urllib.request.urlopen(url).read()
                dest = os.path.join(WORK_DIR, item["filename"])
                with open(dest, "wb") as f:
                    f.write(data)
                print("已保存:", dest, flush=True)


if __name__ == "__main__":
    main()
