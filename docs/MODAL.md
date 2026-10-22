# 在 Modal 上运行

两个核心在 Modal 上的形态：

| 核心 | 移植方式 | 结论 |
| --- | --- | --- |
| A. colab_cli（ComfyUI / MiniMax 工作流） | `--backend modal`：一个任务会话 = 一个 GPU Sandbox，模型放 Volume | **可直接移植**，与 Colab 并存 |
| B1. kcloud（代码生成 + 验证） | `kcloud local --executor modal`：任务在 Modal Sandbox 里跑，模型调用仍直连 Kaggle 模型代理 | **可移植执行环境**，模型额度仍来自 Kaggle |
| B2. kllm 网关 | 不移植 | 它的价值是 Kaggle Benchmarks 每日免费额度，Modal 提供不了；放到 Modal 上只是多一跳。真要脱离 Kaggle，可另用 vLLM 在 Modal GPU 上自托管开源模型（OpenAI 兼容），不在本原型范围 |

## 一次性准备

```bash
pip install modal                     # colab_cli 侧；kaggle_tools 侧: pip install -e "kaggle_tools[modal]"
modal token new                       # 浏览器登录，写入 ~/.modal.toml
modal secret create hf-token HF_TOKEN=hf_xxx   # 可选：需要 HF token 的模型
modal volume create colab-task-models          # 可选：不建则首次运行自动创建
# 预放模型（可选，否则 remote_workflow 会按配置下载并缓存到 Volume）
modal volume put colab-task-models ./models/ /
```

Windows PowerShell 同样适用（`modal` 命令在虚拟环境 Scripts 下）。

## 运行

```bash
# 工作流
python run.py --backend modal --gpu L4 --workflow-config workflows/minimax_h3.json --keep
python run.py --backend modal --session comfyui-h3 --stop      # 停掉保留的 Sandbox

# kcloud：在 Modal 里跑 repo 任务（模型费用照旧走 Kaggle 额度）
kcloud -t repo local --executor modal -r path/to/repo -p "..."
```

可选环境变量：`MODAL_APP_NAME`、`MODAL_MODELS_VOLUME`、`MODAL_SANDBOX_TIMEOUT`（默认 6 h，上限 24 h）、
`MODAL_SECRETS`（逗号分隔 Secret 名）、`MODAL_COMFYUI_REF`、`MODAL_IMAGE`；kcloud：`KCLOUD_MODAL_APP`。
会话映射保存在 `~/.colab_backends/modal/sessions.json`（可用 `COLAB_BACKEND_STATE_DIR` 改）。

## 费用估算（官方价目，2026-10-22，https://modal.com/pricing）

Sandbox 按非抢占价计费：CPU $0.00003942/核/秒，内存 $0.00000667/GiB/秒（列表价 ×3）；GPU 按列表价。

以「4 物理核 + 32 GiB 内存」为例，CPU+内存约 $0.57 + $0.77 ≈ **$1.34/h**，再加 GPU：

| GPU | GPU $/h | 合计约 $/h | 一个 20 分钟视频任务 |
| --- | --- | --- | --- |
| T4 | 0.59 | 1.93 | ≈ $0.64 |
| L4 | 0.80 | 2.14 | ≈ $0.71 |
| A100 40GB | 2.10 | 3.44 | ≈ $1.15 |
| H100 | 3.95 | 5.29 | ≈ $1.76 |

（Modal 按「申请量与实际用量的较大值」计费；Sandbox 默认只申请 0.125 核/128 MiB，上表按 ComfyUI 实际占用估算。）
Starter 计划每月含 $30 计算额度，约等于 L4 上 14 小时。Volume 1 TiB 以内免费，超出 $0.09/GiB/月。
kcloud 的 CPU Sandbox（1 核 + 2 GiB）约 $0.19/h，一次 10 分钟验证约 $0.03。

## 限制

- 首次运行要构建镜像（clone ComfyUI + pip），约 5–10 分钟，之后命中缓存。
- Sandbox 最长 24 h；`--keep` 保留的 Sandbox 只要活着就计费，用完记得 `--stop`。
- 本机 127.0.0.1 上的 kllm 网关 Modal 访问不到，kcloud 在 Modal 里直接用 MODEL_PROXY_URL/API_KEY（token 有有效期，长任务注意）。
- 本原型没有在真实 Modal 账号上验证（没有凭据），只有假 SDK 单测 + modal 1.6.2 SDK 签名核对。
