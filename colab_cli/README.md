# Google Colab CLI (`google-colab-cli`) 详细使用指南

## INSO Wan2.2 Animate 14B

专用交互式入口：

```bash
cd colab_cli
./start_wan_animate.sh
```

使用 `UNETLoader` 测试 Wan Animate 2 BF16 权重：

```bash
./start_wan_animate_an2.sh
```

其 UNET 会优先复用或写入以下 Drive 缓存：

```text
MyDrive/code/backup/models/diffusion_models/wan_animate_2_bf16.safetensors
```

使用 Wan Animate 2 BF16 UNET 和原生 `LoraLoader` 串联版本：

```bash
./start_wan_animate_an2_lora.sh
```

该版本依次加载 `Wan2.2-big`、Relight、细节辅助和 LightX2V LoRA，
默认模型强度分别为 `0.70`、`0.85`、`0.60`、`1.00`，CLIP 强度均为 `0`。

也可以运行 `./start.sh`，然后选择
`inso_wan2_2_14.json`。建议使用 A100；该工作流不支持 TPU。

运行前分别放置工作流引用的参考图和源视频：

- 参考图：`colab_cli/image/78515578_109_5e22.jpg`
- 源视频：`colab_cli/video/打败敌人后要说一声对不起Lolita话题_走拍话题_转生小猫日记话题_女仆装话题.mp4`

公开来源明确的模型会自动下载并缓存到 Google Drive。工作流中的
`Wan2.2-big.safetensors` 无法从文件名唯一确认来源，必须手动放到：

```text
MyDrive/code/backup/models/loras/Wan2.2-big.safetensors
```

程序会在下载其他大模型之前检查此文件，缺失时直接报错。

输出会在远端打包并切成 16 MiB 分块。远端脚本保持运行，本地在 VM
结束前立即通过 Contents API 下载、重组，并执行分块 SHA-256、整包
SHA-256 和 ZIP CRC 校验；本地回传确认后，远端脚本才会退出。若
Contents API 临时不可用，程序会自动通过当前 Jupyter stdout 执行通道
兜底传输，不需要手动操作。

如果任务使用 `--keep` 或 `--reuse` 保留了 VM，也可以只重试兼容下载，
不会重新执行工作流：

```bash
python run.py --session <会话名称> --download-only
```

本文档全面整理了 `google-colab-cli` 的安装配置、核心命令、常用工作流、最佳实践与排错技巧，以及本目录下自动化脚本（ComfyUI / MiniMax H3 工作流）的运行机制。

---

## 目录

1. [核心特性与工作原理](#1-核心特性与工作原理)
2. [安装与认证配置](#2-安装与认证配置)
3. [命令速查与详细用法](#3-命令速查与详细用法)
   - [3.1 会话管理 (Session)](#31-会话管理-session)
   - [3.2 代码执行与交互 (Exec & REPL)](#32-代码执行与交互-exec--repl)
   - [3.3 一次性任务运行器 (colab run)](#33-一次性任务运行器-colab-run)
   - [3.4 文件操作与同步 (Files)](#34-文件操作与同步-files)
   - [3.5 环境配置与自动化 (Tools)](#35-环境配置与自动化-tools)
4. [高级用法与实战技巧](#4-高级用法与实战技巧)
5. [常见问题与踩坑排查 (FAQ)](#5-常见问题与踩坑排查-faq)
6. [本目录实战案例: MiniMax H3 工作流自动化](#6-本目录实战案例-minimax-h3-工作流自动化)

---

## 1. 核心特性与工作原理

`google-colab-cli` 是一个让你在本地终端无需打开浏览器即可管理并使用 Google Colab 云端计算资源的命令行工具。

* **多算力规格支持**：支持开箱即用的 CPU、GPU（`T4`, `L4`, `G4`, `H100`, `A100`）及 TPU（`v5e1`, `v6e1`）。
* **自动化保活 (Keep-Alive)**：内置独立的后台守护进程，自动对云端 VM 发送保活请求，避免因网页关闭或无操作导致实例被 Colab 释放。
* **本地与远端无缝衔接**：支持直接执行本地 Python 脚本、Jupyter Notebook (`.ipynb`)、管道输入流，无需手动反复上传代码。
* **一次性任务运行器 (`colab run`)**：自动申请 VM -> 执行脚本 -> 传回输出 -> 立即销毁 VM，适合 CI/CD 和自动化脚本。
* **完整的文件与环境管理**：基于 `uv` 极速安装依赖，支持双向文件传输、Drive 挂载、在线日志导出与浏览器会话回溯。

---

## 2. 安装与认证配置

### 2.1 安装方式

推荐使用 `uv` 独立工具链安装，也可以直接使用 `pip`：

```bash
# 推荐方式 (uv)
uv tool install google-colab-cli

# 或使用 pip
pip install google-colab-cli
```

> **注意**：目前 Colab CLI 主要支持 **Linux** 和 **macOS** 环境。

### 2.2 认证方式

Colab CLI 支持两种认证模式，通过全局参数 `--auth` 指定（默认为 `adc`）：

#### 方式 A: OAuth2 网页授权模式 (`--auth=oauth2`)
首次运行时会自动调起本地浏览器完成 Google 账号授权：
```bash
colab --auth=oauth2 new -s my-session
```
* 授权凭据将保存在本地 `~/.config/colab-cli/token.json`。
* 若在无头服务器/SSH 环境，需配合 client-oauth-config JSON 配置使用。

#### 方式 B: ADC 模式 (`--auth=adc`，推荐用于自动化/脚本)
对于无头服务器、Agent 或脚本自动化环境，使用 `gcloud` Application Default Credentials 最为稳定可靠。需要配置全部 4 个关键 scope：

```bash
gcloud auth application-default login \
  --scopes=openid,\
https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/userinfo.email,\
https://www.googleapis.com/auth/colaboratory
```

> **为什么必须包含这 4 个 Scope？**
> * `userinfo.email`：用于访问 Colab 会话后台后端（否则报 `401 Unauthorized`）。
> * `colaboratory`：用于 RuntimeService 保活心跳 RPC（否则报 `403 Forbidden`）。
> * `openid` 与 `cloud-platform`：Google Cloud 客户端标准基础权限。

#### 验证认证状态
```bash
# 查看当前活跃会话（可快速验证认证是否有效）
colab sessions

# 查看当前认证的账号、Scope 列表及过期时间（排错利器）
colab whoami
```

---

## 3. 命令速查与详细用法

### 3.1 会话管理 (Session)

| 命令 | 说明 | 常用参数 |
| :--- | :--- | :--- |
| `colab new` | 创建新的计算实例 | `-s <名称>` 指定会话名<br>`--gpu <T4\|L4\|G4\|H100\|A100>`<br>`--tpu <v5e1\|v6e1>` |
| `colab sessions` | 列出当前所有活跃的会话 | 无 |
| `colab status` | 查看指定会话的硬件状态与空闲情况 | `-s <名称>` |
| `colab restart-kernel`| 重启云端 Jupyter 内核（保留已下载的文件） | `-s <名称>` |
| `colab stop` | 销毁云端 VM 并停止保活守护进程 | `-s <名称>` |
| `colab url` | 生成该会话在浏览器中打开的 Colab Web 链接 | `-s <名称>`<br>`--open`（自动打开浏览器） |

#### 示例
```bash
# 启动一台配备 L4 GPU (24GB 显存) 的会话，命名为 comfy-task
colab new -s comfy-task --gpu L4

# 启动一台配备 A100 GPU 的高端会话
colab new -s llm-train --gpu A100

# 查看状态
colab status -s comfy-task

# 任务完成后必须手动销毁，防止扣除计算单元
colab stop -s comfy-task
```

---

### 3.2 代码执行与交互 (Exec & REPL)

#### 1. 执行本地脚本 (`colab exec`)
`colab exec` 会直接在本地读取文件并发送到远端内核执行，**无需手动 `colab upload` 脚本**：

```bash
# 运行本地的 train.py 脚本
colab exec -s my-session -f train.py

# 运行本地 Jupyter Notebook (执行结果将保存到 demo_output.ipynb)
colab exec -s my-session -f demo.ipynb

# 管道传入代码执行
echo "import torch; print('CUDA可用:', torch.cuda.is_available())" | colab exec -s my-session

# 长时间任务（如下载大模型/训练）需指定超时时间（单位秒，传 0 或大数值避免默认 30s 超时）
colab exec -s my-session -f long_task.py --timeout 86400
```

#### 2. 保存输出图片 (`--output-image`)
当远端脚本产生 Matplotlib 图像或 PIL 图片时，可直接捕获到本地：
```bash
colab exec -s my-session -f plot.py --output-image ./result.png
```

#### 3. 交互式控制台与终端
```bash
# 交互式 Python REPL (管道传入 EOF 时会自动退出)
colab repl -s my-session

# 连接到远端真实的交互式 bash/tmux 终端
colab console -s my-session
```

---

### 3.3 一次性任务运行器 (`colab run`)

`colab run` 相当于将 `colab new` -> `colab exec` -> `colab stop` 串联为一条原子命令：
1. 自动根据参数申请对应硬件的 VM；
2. 自动注入命令行参数（通过 `sys.argv` 传递）；
3. 运行完毕后**自动销毁 VM**，防止意外烧计费；
4. 退出状态码（Exit Code）与本地 Python 进程完全保持一致。

```bash
# 在 T4 显卡上执行一次性脚本，跑完即释放
colab run --gpu T4 benchmark.py --batch-size 32

# 在 L4 显卡上运行，并保留 VM 供后续排查（--keep）
colab run --gpu L4 --keep -s debug-session test_model.py
```

#### 支持 Shebang 脚本写法
在脚本头部加上如下声明，赋予可执行权限后即可当作本地脚本直接运行：
```python
#!/usr/bin/env -S colab run --gpu L4
import torch

print("GPU 型号:", torch.cuda.get_device_name(0))
print("显存大小:", torch.cuda.get_device_properties(0).total_memory / 1024**3, "GB")
```
```bash
chmod +x check_gpu.py
./check_gpu.py
```

---

### 3.4 文件操作与同步 (Files)

> 💡 **重要**：Colab VM 的默认工作目录是 `/content`。在进行文件操作时，推荐显式使用绝对路径（如 `/content/data.zip`）。

| 命令 | 说明 | 示例 |
| :--- | :--- | :--- |
| `colab ls` | 查看远端目录文件 | `colab ls -s my-session /content` |
| `colab upload` | 上传本地文件到云端 | `colab upload -s my-session ./weights.pth /content/weights.pth` |
| `colab download` | 从云端下载文件到本地 | `colab download -s my-session /content/output.zip ./output.zip` |
| `colab rm` | 删除云端文件 | `colab rm -s my-session /content/temp.log` |
| `colab edit` | 使用本地 `$EDITOR` 编辑云端文件并同步回写 | `colab edit -s my-session /content/config.yaml` |

---

### 3.5 环境配置与自动化 (Tools)

#### 1. 快速安装依赖 (`colab install`)
远端会优先使用 `uv pip` 极速安装，秒级解析完成：
```bash
# 安装指定包
colab install -s my-session torch torchvision transformers diffusers

# 从 requirements.txt 安装
colab install -s my-session -r requirements.txt
```

#### 2. 挂载 Google Drive (`colab drivemount`)
```bash
# 默认挂载到 /content/drive
colab drivemount -s my-session
```

#### 3. 查看与导出日志 (`colab log`)
```bash
# 查看最近 30 条事件日志
colab log -s my-session -n 30

# 将当前会话历史导出为 Notebook / Markdown 文件
colab log -s my-session -o session_record.ipynb
colab log -s my-session -o session_record.md
```

#### 4. 打开额度充值页面
```bash
colab pay
```

---

## 4. 高级用法与实战技巧

### 4.1 显存（VRAM）与内存（RAM）的选择说明

* **关于系统内存（RAM）**：`colab new` 命令行目前**不提供**独立的 `--ram` 或 `--high-ram` 参数。
* **配置规则**：系统内存大小随硬件规格联动分配。
  * **T4**：标准系统内存（约 12.7 GB RAM）。
  * **L4 / A100**：通常会自动分配 **High-RAM 高内存环境**（系统内存更大，且配有 24GB~80GB 大显存）。
* **大模型显存防爆策略**：运行 30GB+ 大模型时，可在脚本中结合 ComfyUI / PyTorch 的显存优化参数（例如 `--lowvram`、`--reserve-vram` 或 `torch.cuda.empty_cache()`）。

### 4.2 区分 CLI 鉴权 与 `colab auth`

* **CLI 鉴权**（`gcloud auth` 或 `--auth=oauth2`）：控制本地电脑是否有权限管理 Colab 虚拟机。
* **`colab auth` 命令**：为**云端 VM 内部**的 Python 运行时注入 Google Cloud 凭证（例如在 Notebook 里访问 BigQuery 或 GCS 存储桶）。

### 4.3 隔离并行任务配置
如果有多个自动化脚本或 Agent 并发运行，可以通过 `--config` 指定独立的会话状态文件，防止状态覆盖：
```bash
colab --config /tmp/session_worker1.json new -s worker-1 --gpu L4
colab --config /tmp/session_worker2.json new -s worker-2 --gpu A100
```

---

## 5. 常见问题与踩坑排查 (FAQ)

### Q1: 运行 `colab new` 报 `403 Forbidden` 或保活进程退出？
* **原因**：认证凭据中缺少 `https://www.googleapis.com/auth/colaboratory` 作用域。
* **解决**：运行 `colab whoami` 检查 scopes，按[2.2 小节](#方式-b-adc-模式-authadc推荐用于自动化脚本)使用 `gcloud` 重新登录并传入 4 个 scope。

### Q2: 申请 A100 / L4 报错 `400 Bad Request` 或 `ResourceExhausted`？
* **原因**：账号当前层级没有对应的计算单元（Compute Units）或该区域此类 GPU 暂时缺货。
* **解决**：运行 `colab pay` 检查计算单元余额，或降级使用 `--gpu T4` 或 CPU。

### Q3: 为什么 `colab repl` 或 `colab console` 在脚本中卡死？
* **原因**：这两个命令设计用于人类交互，需要 TTY 终端。
* **解决**：在自动化脚本中，统一使用 `colab exec -s <name> -f <file.py>` 或通过管道 `echo "..." | colab exec -s <name>`。

### Q4: 提示 `Session not found`？
* **原因**：远端 VM 可能因空闲超时或云端资源回收被释放。
* **解决**：运行 `colab sessions` 查看当前有效会话，必要时重新 `colab new`。

---

## 6. 本目录实战案例: MiniMax H3 工作流自动化

本目录包含一套配置驱动的 ComfyUI 工作流调度器，MiniMax H3 是默认工作流：

```
colab_cli/
├── start.sh                    # 交互选择工作流和运行配置
├── stop.sh                     # 交互停止会话
├── set_frames.py               # 一键设置首帧/尾帧输入图片
├── run.py                      # VM 生命周期、配置/输入上传、结果下载
├── remote_setup.py             # 仅安装通用 ComfyUI 环境
├── remote_workflow.py          # 配置驱动的模型准备和工作流执行器
└── workflows/
    └── minimax_h3.json         # MiniMax H3 API、输入规则和模型清单
```

### 设置输入图片

运行前先用 `set_frames.py` 指定输入图片，它会自动把图片复制到
`image/` 目录（同名不同内容自动加序号后缀），并在 `api.json` 中追踪
`MiniMaxH3ImageToVideo` 节点的帧输入，更新对应 `LoadImage` 节点引用的
文件名：

仓库根 `api.json` 已接首帧（节点 114）和尾帧（节点 122）：

```bash
python3 colab_cli/set_frames.py --first 首帧.png
python3 colab_cli/set_frames.py --first 首帧.png --last 尾帧.png
```

未接线的工作流会被明确拒绝。

之后正常 `./start.sh` 选择 `minimax_h3.json` 即可，输入图片会随工作流
自动上传。

### 运行流程

```mermaid
sequenceDiagram
    autonumber
    actor User as 本地用户 / 终端
    participant Local as colab_cli/run.py
    participant CLI as google-colab-cli
    participant VM as Colab 云端 VM (L4/A100)

    User->>Local: 选择新建 VM 或复用 VM
    alt 新建 VM
        Local->>CLI: colab new -s comfyui-h3 --gpu L4
        CLI->>VM: 申请算力实例并启动保活守护
        Local->>CLI: colab exec -f remote_setup.py
        Note over VM: 克隆 ComfyUI 并安装通用依赖
    else 复用 VM
        Local->>CLI: colab status -s comfyui-h3
        Note over VM: 保留已有 ComfyUI、模型和 Drive 挂载
    end
    Local->>CLI: 上传工作流配置、api.json 和输入素材
    Local->>CLI: colab exec -f remote_workflow.py
    Note over VM: 按配置准备模型和输入<br/>启动 ComfyUI 并执行 API 工作流<br/>打包 output.zip
    Local->>CLI: colab download /content/output.zip
    CLI->>Local: 下载到 colab_output/任务编号/output.zip
    opt 未要求保留 VM
        Local->>CLI: colab stop -s comfyui-h3 (释放资源)
    end
```

### 常用运行命令

```bash
# 交互式启动（每次选择工作流、新建/复用 VM、GPU/TPU、Drive 缓存等）
./colab_cli/start.sh

# 交互式停止指定会话
./colab_cli/stop.sh

# 1. 默认运行: 自动挂载 Google Drive, 优先从 code/backup/models/ 复用 39GB 模型
python colab_cli/run.py --gpu L4

# 2. 在 A100 (40GB/80GB 显存) 上运行
python colab_cli/run.py --gpu A100

# 3. 不挂载 Google Drive (纯 VM 临时下载)
python colab_cli/run.py --gpu L4 --no-mount-drive

# 4. 调试模式: 跑完不销毁 VM，方便排查
python colab_cli/run.py --gpu L4 --keep

# 5. 复用仍在运行的会话，跳过创建 VM 和 ComfyUI 通用安装；任务后继续保留 VM
python colab_cli/run.py --session comfyui-h3 --reuse

# 6. 手动销毁会话
python colab_cli/run.py --stop

# 7. 直接选择另一个工作流配置
python colab_cli/run.py --workflow-config colab_cli/workflows/example.json --gpu L4

# 8. 为已经适配 PyTorch/XLA 或 JAX 的工作流申请 TPU
python colab_cli/run.py --workflow-config colab_cli/workflows/tpu_example.json --tpu v6e1
```

需要连续提交任务时，第一次使用新建模式并选择保留 VM；之后再次运行 `start.sh`，选择同一会话的复用模式即可。复用模式会重新上传本次 API 工作流和输入素材，但不会再次运行 `remote_setup.py`，任务结束后也不会自动关闭 VM。复用时只检测 Google Drive 是否已经挂载，不会调用 `drive.mount()` 或再次触发认证；如果挂载已经失效，则自动改用 VM 本地模型。全部任务结束后使用 `stop.sh` 释放资源。

云端 `/content/output` 会在每次推理前重建，因此压缩包只包含当前任务的结果。本地结果按唯一任务编号保存到 `colab_output/<任务编号>/output.zip`，不会覆盖上一次下载。

推理期间的资源监控会在后台每秒采集一次 NVIDIA GPU 使用率，并每 2 秒刷新资源栏。GPU 状态同时显示当前值、最近 30 秒平均值和峰值，避免 ComfyUI 步骤间隙的瞬时 `0%` 造成误判。

MiniMax H3 当前只支持 GPU。ComfyUI 的设备管理没有 PyTorch/XLA 后端，MiniMax H3 节点及相关算子也以 CUDA/PyTorch 为目标，因此选择 TPU 会在创建 VM 前被兼容性校验阻止。真正的 TPU 工作流需要单独适配 `torch_xla` 或 JAX、移除 CUDA 专用算子并提供相应执行器；不能只把配置中的支持类型改成 `tpu`。

### 新增其他工作流

在 `colab_cli/workflows/` 中增加一个 JSON 文件即可出现在 `start.sh` 菜单中。配置字段：

- `name`：运行日志中的工作流名称；
- `api_json`：相对配置文件所在目录或绝对路径；
- `supported_accelerators`：工作流实际支持的加速器类型，可选值为 `gpu`、`tpu`、`cpu`；省略时默认为 `gpu`；
- `accelerator_note`：选择不兼容加速器时显示的工作流专属说明；
- `input_fields`：需要从 API 工作流读取并上传的输入节点字段；
- `editable_fields`：任务提交页允许覆盖的 API 节点输入白名单；可按唯一
  `class_type` 或明确的 `node_id` 定位节点，MiniMax H3 用它暴露视频 prompt；
- `models`：模型的 Hugging Face 仓库、仓库内文件、ComfyUI 模型目录、Drive 缓存路径及可选精确大小；
- `comfy_flags`：该工作流专属的 ComfyUI 启动参数。

无需模型的工作流可将 `models` 设置为空列表。通用安装逻辑无需复制或修改。

### Google Drive 39GB 模型智能缓存机制
脚本默认已集成 Google Drive 缓存复用逻辑：
1. **只读检测**：工作流执行器检查 `/content/drive` 是否已挂载且 `MyDrive` 可访问，不主动调用 `drive.mount()`；
2. **优先读取**：挂载可用时检查 `/content/drive/MyDrive/code/backup/models/` 对应子目录下的模型文件；
3. **快速链接**：若 Drive 中已有且大小校验通过的模型，自动创建软链接（`os.symlink`）至 ComfyUI 目录；
4. **自动备份**：若 Drive 中尚未存在该模型，则从 HuggingFace 下载后自动复制保存到 Drive 对应文件夹，下次开机即可秒级复用；
5. **无认证回退**：复用 VM 时若 Drive 未挂载或不可访问，不弹出认证，直接使用 VM 本地模型或重新下载。

## 可靠性相关环境变量（Runner / colab_core）

| 变量 | 缺省 | 说明 |
| :--- | :--- | :--- |
| `COLAB_SESSION_QUEUE_MAX` | 5 | 同一会话的 FIFO 排队上限（不含运行中的那个）；0 = 禁用排队，恢复 409 SESSION_BUSY |
| `COLAB_RETRY_ATTEMPTS` | 3 | 上传 / 环境安装 / 产物下载遇瞬时故障的最大尝试次数 |
| `COLAB_RETRY_BASE_SECONDS` | 5 | 指数退避首次等待秒数（封顶 60s） |
| `COLAB_REATTACH_TIMEOUT` | 1800 | 执行阶段断线后等待远端完成的最长秒数；0 = 不重连 |
| `COLAB_REATTACH_POLL_SECONDS` | 30 | 重连期间轮询远端产物清单的间隔 |

提交任务时请求体可带 `"queue": false`，同会话忙时直接返回 409 而不排队。

## Docker 一键启动（Web + Runner + MySQL）

一条 `docker compose` 同时起三个容器：`web`（Nuxt，映射宿主 3000）、`runner`（Python Runner，**不映射宿主端口**，
只在 compose 网络里被 web 访问）、`mysql`（8.4，数据存在卷 `mysql_data`）。web 启动时自动跑数据库迁移（含 012/013）。
Runner 产物卷 `colab_output` 在两个容器里挂在同一路径 `/data/colab_output`。

Windows（Docker Desktop，PowerShell）：

```powershell
cd E:\code\colab_task_list\colab_cli
Copy-Item .env.docker.example .env.docker     # 用记事本填 RUNNER_TOKEN / JWT_SECRET / DB_PASSWORD / DB_ROOT_PASSWORD / ADMIN_PASSWORD
docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker ps       # 三个服务都应为 healthy
start http://127.0.0.1:3000
```

Linux / macOS 把 `Copy-Item` 换成 `cp`，其余相同。

- **Colab 登录**：登录态存在 `./secrets/colab`（挂到容器 `/root/.config/colab-cli`，里面是 `token.json`）。首次运行
  `docker compose --env-file .env.docker exec -it runner colab sessions`，按提示在浏览器打开链接并粘贴授权码，token 会持久化在该目录。
  已在宿主登录过的，也可以直接把宿主 `~/.config/colab-cli/` 的内容复制进去。
- **Kaggle / Modal**：在 `.env.docker` 填 `KAGGLE_USERNAME`/`KAGGLE_KEY`、`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`，并设 `INSTALL_BACKENDS=1` 后重新 `--build`。
- 局域网访问：`WEB_BIND=0.0.0.0`；走 HTTPS 反代时把 `AUTH_COOKIE_SECURE=true`。
- 常用：`docker compose --env-file .env.docker logs -f runner`；停止 `down`（加 `-v` 会删除数据库和产物卷，慎用）。
- Runner 在容器内绑 `0.0.0.0` 需要显式 `COLAB_RUNNER_HOST=0.0.0.0` + `COLAB_RUNNER_ALLOW_NON_LOOPBACK=1`（compose 已设置）；
  直接 `python -m colab_runner` 时默认仍只绑 127.0.0.1。
