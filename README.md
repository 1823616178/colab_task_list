# kaggle-code

Kaggle 使用示例项目：CLI、kagglehub、Kernels(云端 Notebook)、Benchmarks(托管 LLM 调用)。

详细操作手册见 [操作手册.md](操作手册.md)。

## 环境准备

```bash
pip install -r requirements.txt
```

认证：把 `kaggle.json` 或 `access_token` 放到 `~/.kaggle/`(权限 600)。
验证：`kaggle quota` 能显示 GPU/TPU 配额即成功。

## 目录结构

```
colab_cli/                 # Google Colab CLI 云端算力自动化 (ComfyUI / MiniMax H3)
demo/
├── kagglehub_tutorial/   # kagglehub 库示例 01~07(下载数据集/模型/竞赛数据等)
├── my_kernel/            # 推送脚本到云端 Notebook 的完整示例
├── codegen_kernel/       # GPU + Gemma 生成代码的 kernel 示例
└── benchmarks/           # Kaggle Benchmarks 任务示例(调用托管 LLM)
```
Colab CLI 的完整使用指南见 [colab_cli/README.md](colab_cli/README.md)。


## Kaggle Benchmarks:使用 AI 额度调用托管模型

AI 额度(每天 $10 / 每月 $100)仅用于 Benchmarks, 可调用平台托管的
Claude / GPT / Gemini / DeepSeek 等模型(用 `kaggle b t models` 查看列表)。

有**两条独立的使用路线**, 按需二选一:

### 路线 A: 本地执行(不上传任何文件)

只在本地跑任务、花 AI 额度调模型, 脚本不需要上传到平台:

```bash
cd demo/benchmarks

# 1. 初始化: 生成 .env(内含模型代理地址 + 临时密钥, 仅 1 小时有效, 过期重跑本命令)
#    同时生成示例任务 example_task.py
kaggle benchmarks init -y

# 2. 把 .env 导入环境变量(Python 不会自动读取 .env)
set -a && source .env && set +a

# 3. 像普通 Python 脚本一样直接运行
#    通过 Kaggle 模型代理调用 .env 里 LLM_DEFAULT 指定的模型, 消耗 AI 额度
python example_task.py
```

- `.env` 只用于本地鉴权, 不会也不应该被上传
- 默认模型由 `.env` 的 `LLM_DEFAULT` 决定(当前为 google/gemini-3-flash-preview)
- `.run()` 会在本地生成运行记录文件

**认证过期怎么办:**

- 报错 `Missing environment variables for Kaggle authentication` 或密钥过期时,
  重新执行 `kaggle benchmarks init -y` 刷新 `.env`, 然后重新 `source .env` 再跑脚本
- `kaggle benchmarks auth` 也能刷新, 但它是交互式的(问 Y/n), 脚本/管道里会卡死,
  推荐统一用 `init -y`
- 注意 `init -y` 会覆盖同目录的 `example_task.py`, 自己改过的示例先备份

### 路线 B: 云端执行(上传任务, 正式发布)

把任务发布到 Kaggle 平台, 在云端跑、结果计入排行榜:

```bash
cd demo/benchmarks

# 1. 上传任务脚本(只上传 .py; .env 不上传, 云端凭据由平台自动注入)
#    任务名 = 脚本里 @kbench.task(name=...) 规范化后的 slug
#    例: name="What is Kaggle?" -> what-is-kaggle
kaggle b t push what-is-kaggle -f example_task.py

# 2. 指定模型执行(-m 可重复传多个; --wait 等待结果, 单位秒)
kaggle b t run what-is-kaggle -m gemini-3-flash-preview --wait 300

# 3. 查看结果
kaggle b t status what-is-kaggle              # 状态和分数
kaggle b t log what-is-kaggle                 # 日志
kaggle b t download what-is-kaggle -p ./out   # 下载运行记录
# 网页: https://www.kaggle.com/benchmarks/tasks/<用户名>/
```

- 改了脚本要重新 `push`(自动累加版本)再 `run`
- 脚本中 `kbench.llm` 是占位符, 云端由 `-m` 指定实际模型

### 两条路线共同的注意事项

- 脚本里必须调用 `.run()` 或 `.evaluate()`, 否则不会生成运行记录
- 地区限制: 若账号被判定为不受支持地区(如 HK), 本地和云端调用都会报
  `User location "HK" is not supported`(限制在代理服务端, 两条路都受影响), 需先解决属地问题

## Kernels:推送脚本到云端 Notebook

```bash
# 目录内需有: 代码文件 + kernel-metadata.json(见 demo/my_kernel/)
kaggle kernels push -p demo/my_kernel
kaggle kernels status <用户名>/<slug>
kaggle kernels output <用户名>/<slug> -p out/
```

详细字段说明和踩坑记录见 [操作手册.md](操作手册.md) 第 4、5 节。
