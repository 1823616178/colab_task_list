#!/usr/bin/env bash
# =============================================================================
# 在 Kaggle Notebook 上一键部署 ComfyUI 并运行 api.json (Wan2.2 Animate 14B 工作流)
#
# 前置准备:
#   1. 新建一个 Kaggle Dataset, 上传以下文件:
#      - api.json                          (本仓库里的工作流文件)
#      - 78515578_109_5e22.jpg             (参考图, 文件名必须与工作流一致)
#      - 打败敌人后要说一声对不起...mp4       (输入视频, 文件名必须与工作流一致)
#      - 本脚本 setup_and_run.sh           (可选, 也可以把脚本内容直接粘到 cell)
#   2. 新建 Notebook: 右侧设置开启 GPU (T4 x2) 和 Internet, 并 Add Data 上面的 Dataset
#   3. 在 cell 中执行:
#        !bash /kaggle/input/<你的dataset名>/setup_and_run.sh
#
# 说明:
#   - ComfyUI 装在 /kaggle/tmp (不占 20GB 的输出配额), 只有生成的结果会
#     复制到 /kaggle/working/output 供下载
#   - 模型共约 15GB, 首次运行下载耗时较长; T4 上 37 帧采样很慢, 请耐心等待
# =============================================================================
set -euo pipefail

BASE_DIR="${BASE_DIR:-/kaggle/tmp}"          # ComfyUI 安装位置
WORK_DIR="${KAGGLE_WORK_DIR:-/kaggle/working}" # 结果输出位置
COMFY="$BASE_DIR/ComfyUI"
PORT=8188

# ---------- 1. 找到 api.json 和输入素材 ----------
echo ">>> [1/6] 查找 api.json 与输入素材"
API_JSON="$(find /kaggle/input "$(dirname "$0")" -maxdepth 3 -name 'api.json' 2>/dev/null | head -1 || true)"
if [ -z "$API_JSON" ]; then
  echo "ERROR: 找不到 api.json, 请把它放进 Kaggle Dataset" >&2; exit 1
fi
echo "使用工作流: $API_JSON"

# ---------- 2. 克隆 ComfyUI 并安装依赖 ----------
echo ">>> [2/6] 安装 ComfyUI 本体"
if [ ! -d "$COMFY" ]; then
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git "$COMFY"
fi
cd "$COMFY"
pip install -q -r requirements.txt
pip install -q websocket-client "huggingface_hub[hf_transfer]"

# ---------- 3. 克隆工作流用到的全部自定义节点 ----------
echo ">>> [3/6] 安装自定义节点"
CUSTOM_NODES=(
  "https://github.com/ltdrdata/ComfyUI-Manager"                 # 管理器(可选, 方便排查)
  "https://github.com/rgthree/rgthree-comfy"                    # Any Switch / Power Lora Loader
  "https://github.com/ltdrdata/ComfyUI-Impact-Pack"             # BboxDetectorSEGS / SegsToCombinedMask ...
  "https://github.com/ltdrdata/ComfyUI-Impact-Subpack"          # UltralyticsDetectorProvider
  "https://github.com/Fannovel16/comfyui_controlnet_aux"        # DWPreprocessor
  "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite"     # VHS_LoadVideo / VHS_VideoCombine
  "https://github.com/kijai/ComfyUI-KJNodes"                    # ImageResizeKJv2 / PathchSageAttentionKJ / BlockifyMask / DrawMaskOnImage
  "https://github.com/city96/ComfyUI-GGUF"                      # UnetLoaderGGUF
  "https://github.com/pythongosssss/ComfyUI-Custom-Scripts"     # MathExpression|pysssss
  "https://github.com/yolain/ComfyUI-Easy-Use"                  # easy indexAnything
  "https://github.com/chflame163/ComfyUI_LayerStyle"            # LayerUtility: PurgeVRAM
  "https://github.com/chflame163/ComfyUI_LayerStyle_Advance"    # LayerUtility: PurgeVRAM V2
  "https://github.com/1038lab/ComfyUI-RMBG"                     # ClothesSegment
  "https://github.com/jamesWalker55/comfyui-various"            # JWImageResizeByShorterSide
  "https://github.com/melMass/comfy_mtb"                        # PixelPerfectResolution
)
mkdir -p custom_nodes
cd custom_nodes
for repo in "${CUSTOM_NODES[@]}"; do
  name="$(basename "$repo")"
  if [ ! -d "$name" ]; then
    git clone --depth 1 "$repo" "$name" || echo "WARN: clone 失败 $repo"
  fi
done
# 安装各节点的 python 依赖
for req in */requirements.txt; do
  [ -f "$req" ] && pip install -q -r "$req" || true
done
cd "$COMFY"

# ---------- 4. 下载模型 ----------
echo ">>> [4/6] 下载模型 (约 15GB)"
python - <<'PYEOF'
import os, shutil
from huggingface_hub import hf_hub_download

os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
COMFY = os.path.join(os.environ.get("BASE_DIR", "/kaggle/tmp"), "ComfyUI")

# (HF仓库, 仓库内路径, 存放目录)
DOWNLOADS = [
    # 主模型: Wan2.2 Animate 14B GGUF (Q2_K 约 6.5GB)
    ("QuantStack/Wan2.2-Animate-14B-GGUF", "Wan2.2-Animate-14B-Q2_K.gguf",
     f"{COMFY}/models/diffusion_models"),
    # 文本编码器
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/text_encoders/umt5_xxl_fp16.safetensors",
     f"{COMFY}/models/text_encoders"),
    # VAE
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/vae/wan_2.1_vae.safetensors",
     f"{COMFY}/models/vae"),
    # CLIP Vision
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/clip_vision/clip_vision_h.safetensors",
     f"{COMFY}/models/clip_vision"),
    # 人脸检测器 (Impact-Subpack, 放在 models/ultralytics/bbox/)
    ("Bingsu/adetailer", "face_yolov8m.pt",
     f"{COMFY}/models/ultralytics/bbox"),
    # DWPreprocessor 预处理模型 (controlnet_aux 的 ckpts 目录)
    ("hr16/yolox-onnx", "yolox_l.torchscript.pt",
     f"{COMFY}/custom_nodes/comfyui_controlnet_aux/ckpts/hr16/yolox-onnx"),
    ("hr16/DWPose-TorchScript-BatchSize5", "dw-ll_ucoco_384_bs5.torchscript.pt",
     f"{COMFY}/custom_nodes/comfyui_controlnet_aux/ckpts/hr16/DWPose-TorchScript-BatchSize5"),
]
for repo, filename, dest in DOWNLOADS:
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, os.path.basename(filename))
    if os.path.exists(target):
        print(f"已存在, 跳过: {filename}")
        continue
    print(f"下载 {repo}/{filename}")
    path = hf_hub_download(repo, filename)
    shutil.copy(path, target)
print("全部模型下载完成")
PYEOF

# ---------- 5. 拷贝输入素材到 ComfyUI/input ----------
echo ">>> [5/6] 拷贝输入素材"
mkdir -p "$COMFY/input"
find /kaggle/input -maxdepth 3 -type f \
  \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.webm' \
     -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) \
  -exec cp -n {} "$COMFY/input/" \; 2>/dev/null || true
ls -lh "$COMFY/input"

# ---------- 6. 启动 ComfyUI 并通过 API 运行工作流 ----------
echo ">>> [6/6] 启动 ComfyUI 并运行工作流"
mkdir -p "$WORK_DIR/output"
ln -sfn "$WORK_DIR/output" "$COMFY/output_link"

# 后台启动 (T4 16GB 显存较紧, 如 OOM 可追加 --lowvram)
nohup python main.py --port "$PORT" --disable-auto-launch \
  > "$WORK_DIR/comfyui.log" 2>&1 &
SERVER_PID=$!

# 等待服务就绪
for i in $(seq 1 120); do
  curl -sf "http://127.0.0.1:$PORT/system_stats" > /dev/null && break
  sleep 5
done
echo "ComfyUI 已启动 (PID=$SERVER_PID)"

# 提交工作流并等待结果
API_JSON_PATH="$API_JSON" OUT_DIR="$WORK_DIR/output" PORT="$PORT" python - <<'PYEOF'
import json, os, time, shutil, urllib.request

port = os.environ["PORT"]
out_dir = os.environ["OUT_DIR"]
wf = json.load(open(os.environ["API_JSON_PATH"]))

def post(path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))

prompt_id = post("/prompt", {"prompt": wf})["prompt_id"]
print("prompt_id:", prompt_id, flush=True)

while True:
    time.sleep(10)
    history = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/history/{prompt_id}"))
    if prompt_id not in history:
        print("排队/执行中...", flush=True)
        continue
    entry = history[prompt_id]
    status = entry.get("status", {})
    if status.get("status_str") == "error":
        print("执行出错:", json.dumps(status.get("messages", []), ensure_ascii=False))
        raise SystemExit(1)
    if status.get("completed"):
        break

print("执行完成, 收集输出文件")
for node_out in entry.get("outputs", {}).values():
    for key in ("images", "gifs", "videos"):
        for item in node_out.get(key, []):
            url = (f"http://127.0.0.1:{port}/view?filename={item['filename']}"
                   f"&subfolder={item.get('subfolder','')}&type={item.get('type','output')}")
            data = urllib.request.urlopen(url).read()
            dest = os.path.join(out_dir, item["filename"])
            with open(dest, "wb") as f:
                f.write(data)
            print("已保存:", dest)
print("全部完成, 输出目录:", out_dir)
PYEOF

kill $SERVER_PID 2>/dev/null || true
echo ">>> 完成! 结果在 $WORK_DIR/output, ComfyUI 日志在 $WORK_DIR/comfyui.log"
