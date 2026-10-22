# 计算后端：Colab / Kaggle / Modal

同一套 ComfyUI / MiniMax 工作流（`colab_cli/workflows/*.json` + `remote_setup.py` + `remote_workflow.py`）
现在可以选择跑在三种云上。**Colab 仍是默认，原有行为不变。**

## 怎么选

| 入口 | 写法 |
| --- | --- |
| CLI | `python run.py --backend modal --gpu A100 ...`（`--backend colab|kaggle|modal`） |
| 环境变量 | `COMPUTE_BACKEND=kaggle`（未显式指定时生效） |
| Runner API | `POST /tasks` 请求体加 `"backend": "modal"` |
| Web 控制台 | 新建任务 →「计算后端」下拉；任务列表/详情显示后端（DB 迁移 013 新增 `tasks.backend`，旧任务默认 colab） |

GPU 规格按后端校验（`colab_core/config.py: BACKEND_GPU_SPECS`，前端 `web/utils/computeBackends.ts` 同步）：

| 后端 | GPU | TPU |
| --- | --- | --- |
| colab | T4, L4, G4, H100, A100 | v5e1, v6e1 |
| kaggle | T4 / T4x2（Kaggle 的 T4 实为 2×T4）, P100 | — |
| modal | T4, L4, A10, L40S, A100, A100-80GB, H100, H200, B200 | — |

## 架构

`colab_transport.base.Transport` 就是后端接口（create_session / mount_drive / upload / exec_script /
exec_workflow / download_output / session_status / check_session_readable / stop_session /
remote_output_ready）。`get_transport(mode, sink, backend=...)` 按后端返回实现：

| 后端 | 实现 | 会话 | 模型盘（Drive 路径 `/content/drive/MyDrive/code/backup/models`） | 输出 |
| --- | --- | --- | --- | --- |
| colab | `native.py` / `cli.py`（原样） | Colab VM | Google Drive | Contents API / 执行流 |
| kaggle | `kaggle_backend.py` | 本地暂存目录 + 一个私有 script kernel | `KAGGLE_MODELS_DATASET` 挂到 /kaggle/input 后逐个软链 | `kernels output` 下载 manifest+分块 |
| modal | `modal_backend.py` | 一个 GPU `modal.Sandbox` | `modal.Volume` 直接挂到该路径 | Sandbox filesystem 读 manifest+分块 |

Kaggle/Modal 都以 `REMOTE_OUTPUT_NO_WAIT=1` 运行 `remote_workflow.py`：写完
`/content/output_transfer/manifest.json` 与分块后不等本地 ACK 直接退出，本地按清单重组并校验
SHA-256 与 ZIP CRC（复用 `transfer.assemble_output_archive`，与 Colab 同一套校验）。
`PROGRESS:` / `METRICS:` 结构化行照常转成 `workflow.exec` / `metrics.sample` 事件。

## 各后端准备

### Colab（默认）
不变：`google-colab-cli` 登录，见 colab_cli/README。

### Kaggle
1. `pip install kaggle`（或装 kaggle_tools，Kaggle 后端会复用 `kaggle_tools.common.kaggle_cli` 的超时与报错）。
2. 凭据：`~/.kaggle/kaggle.json` 或 `KAGGLE_USERNAME` + `KAGGLE_KEY`。
3. 账号完成**手机验证**（GPU 和 Internet 都需要；ComfyUI 安装与 HF 模型下载要联网）。
4. 可选：把常用模型传成私有 Dataset，`KAGGLE_MODELS_DATASET=<用户>/<slug>`，省去每次下载。
5. 其他可选变量：`KAGGLE_INLINE_MAX_BYTES`（默认 1.5 MB，超过则输入走私有 Dataset `<用户>/colab-<会话>-inputs`）、
   `KAGGLE_POLL_SECONDS`（默认 30）、`KAGGLE_ENABLE_INTERNET`（默认 1）。

运行：`python run.py --backend kaggle --gpu T4 --workflow-config workflows/minimax_h3.json`

### Modal
见 [MODAL.md](MODAL.md)。运行：`python run.py --backend modal --gpu L4 ...`

## 对比

| | Colab | Kaggle | Modal |
| --- | --- | --- | --- |
| 计费 | 计算单元（Pay As You Go 约 $9.99/100 CU；Pro 订阅） | 免费 | 按秒；Starter 每月含 $30 |
| 示例单价 | 依 GPU 每小时消耗不同 CU | 0 | T4 $0.59/h，L4 $0.80/h，A100-40G $2.10/h，H100 $3.95/h（仅 GPU） |
| 配额/限制 | CU 用完即停，空闲会断开 | GPU 每周约 30 h（以 Kaggle 显示为准），单次会话最长 12 h | 按预算；Sandbox 最长 24 h；Starter GPU 并发 10 |
| 交互性 | 实时日志、可复用 VM | 批处理：日志在 kernel 结束后一次性取回；没有入站网络 | 实时日志、可复用 Sandbox |
| 冷启动 | 分配 VM + 每次 pip 安装 | 排队 + 每次 pip 安装 | 首次构建镜像 5–10 min，之后秒级 |
| 输入大小 | 直接上传 | 内嵌 ≤1.5 MB，否则私有 Dataset（新版本有传播延迟） | 直接上传 |
| 模型缓存 | Google Drive | 私有 Dataset（只读，新模型写到可写目录） | Volume（1 TiB 内免费） |
| 适合 | 现有日常流程 | 不赶时间、想省钱的批量任务 | 要大显存/快速、稳定的任务 |

## 已知限制（原型）

- Kaggle：没有实时进度（kernel 运行期间只有状态轮询事件）；取消任务不会停掉已推送的 kernel，需在 kaggle.com 手动停；
  `--accelerator` 取值（`NvidiaTeslaT4` / `NvidiaTeslaP100`）来自 kaggle CLI 2.x，Kaggle 若改名需同步 `KAGGLE_GPU_MACHINES`；
  同一会话重复推送会生成新版本 kernel。
- Modal：未在真实账号上跑过（本次没有凭据），只用假 SDK 做了全流程单测；需要 `modal token new` 后先跑一次小任务验证。
- Web「会话」页仍只管理 Colab 会话；Kaggle/Modal 会话用 `run.py --backend ... --stop --session X` 停止。

## 来源（2026-10-22 查阅）

- Modal 定价：https://modal.com/pricing
- Modal Sandbox / Volume / GPU 文档：https://modal.com/docs/guide/sandbox ，https://modal.com/docs/guide/volumes ，https://modal.com/docs/guide/gpu
- Kaggle Notebooks 限制：https://www.kaggle.com/docs/notebooks
- Kaggle API（kernels push/status/output）：https://github.com/Kaggle/kaggle-api
- Colab 计算单元：https://colab.research.google.com/signup
