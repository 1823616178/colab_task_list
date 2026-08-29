# Kaggle CLI 项目命令参考

本文档面向 `cloud_codegen` 项目，基于项目固定的 `kaggle==2.2.4`，只介绍本项目实际使用的
认证、Model Proxy、Dataset 和 Benchmark Task 命令。竞赛提交、Notebook、模型仓库等
其他 Kaggle CLI 功能不在本文范围内。

官方参考：

- [Kaggle CLI](https://github.com/Kaggle/kaggle-cli)
- [Authentication](https://github.com/Kaggle/kaggle-cli/blob/main/skills/references/auth.md)
- [Datasets](https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets.md)
- [Benchmarks](https://github.com/Kaggle/kaggle-cli/blob/main/docs/benchmarks.md)

不同版本的参数可能变化。遇到本文与本机输出不一致时，以本机 `--help` 为准：

```bash
kaggle --version
kaggle --help
kaggle datasets --help
kaggle benchmarks tasks --help
```

## 1. 命令层级与别名

本文使用的命令树：

```text
kaggle
├── quota
├── auth
│   ├── login
│   ├── print-access-token
│   └── revoke
├── config
│   └── view
├── datasets
│   ├── init
│   ├── create
│   ├── version
│   ├── status
│   ├── files
│   ├── metadata
│   ├── download
│   └── delete
└── benchmarks (b)
    ├── auth
    ├── init
    └── tasks (t)
        ├── models
        ├── list
        ├── push
        ├── run
        ├── status
        ├── log
        ├── download
        ├── delete
        └── publish
```

以下写法等价：

```bash
kaggle benchmarks tasks models
kaggle b t models
```

## 2. 安装与版本

项目固定版本：

```bash
python -m pip install "kaggle==2.2.4"
kaggle --version
```

升级 Kaggle CLI 前，应先核对 `run_cloud.py` 中使用的 `benchmarks tasks download -o`、
Dataset 目录模式和模型名称是否仍兼容。

### 2.1 `kaggle quota` 不是模型额度

项目锁定的 2.2.4 支持顶层额度命令：

```bash
kaggle quota
kaggle quota --format json
kaggle quota --format csv
kaggle quota --csv
```

| 参数 | 含义 |
|---|---|
| `-v`, `--csv` | 使用 CSV 输出 |
| `--format FORMAT` | 使用 `table`、`csv` 或 `json` 输出 |

`--csv` 和 `--format` 互斥，二选一。这个命令显示的是每周 GPU/TPU accelerator 时长，
**不是** Model Proxy 的 AI 推理额度。

## 3. Kaggle 账号认证

Kaggle CLI 和 Model Proxy 是两层凭据：

```text
Kaggle 账号凭据       用于 Dataset、Task、状态和下载等 API
MODEL_PROXY_API_KEY  用于本地 Benchmark Task 调用 LLM，通常是短期凭据
```

### 3.1 `kaggle auth login`

浏览器 OAuth 登录：

```bash
kaggle auth login
```

参数：

| 参数 | 含义 | 何时使用 |
|---|---|---|
| `--no-launch-browser` | 不自动打开浏览器，只打印登录地址 | SSH、容器、无桌面环境 |
| `--force` | 即使已有登录记录也重新认证 | 切换账号或修复过期认证 |

参数组合：

```bash
# 普通桌面环境
kaggle auth login

# 远程服务器，把打印的 URL 放到本地浏览器打开
kaggle auth login --no-launch-browser

# 强制重新登录当前机器
kaggle auth login --force

# 远程服务器并强制重新登录
kaggle auth login --no-launch-browser --force
```

### 3.2 非交互认证来源

CLI 也会读取：

```text
KAGGLE_API_TOKEN
~/.kaggle/access_token
~/.kaggle/kaggle.json
KAGGLE_USERNAME + KAGGLE_KEY
```

自动化环境优先使用 `KAGGLE_API_TOKEN`，不要把 token 写进仓库：

```bash
export KAGGLE_API_TOKEN="从安全的 Secret 管理器读取"
kaggle config view
```

### 3.3 `kaggle auth print-access-token`

从已有 OAuth 登录生成访问 token：

```bash
kaggle auth print-access-token
```

可指定有效期：

```bash
kaggle auth print-access-token --expiration 30m
kaggle auth print-access-token --expiration 6h
kaggle auth print-access-token --expiration 2d
```

`--expiration` 接受一个正整数加单位：`s`、`m`、`h`、`d`、`w`。不要在终端日志、聊天、
CI 输出或版本库里打印和保存返回值。

注意：2.2.4 的 `--expiration` 帮助文本、解析实现和官方新文档存在不一致。如果自定义有效期
报 `TypeError` 或 `Invalid duration format`，不要反复尝试不同格式；先省略该参数使用默认
有效期。升级 CLI 前按第 2 节重新做兼容性检查。

### 3.4 `kaggle auth revoke`

撤销当前 OAuth refresh token：

```bash
kaggle auth revoke
kaggle auth revoke --reason "rotate credentials"
```

撤销后，依赖这份 OAuth 登录的命令需要重新执行 `kaggle auth login`。

### 3.5 `kaggle config view`

检查 CLI 当前能否识别账号：

```bash
kaggle config view
```

`run_cloud.py dataset` 在没有传 `--owner` 时，会尝试从这个输出中提取 `username:`。
如果使用的 token 模式没有显示用户名，应显式传 `--owner`。

## 4. Model Proxy 凭据

### 4.1 `kaggle benchmarks auth`

只申请 Model Proxy 凭据：

```bash
kaggle benchmarks auth -y
```

别名：

```bash
kaggle b auth -y
```

参数：

| 参数 | 含义 |
|---|---|
| `-y`, `--yes` | 跳过确认提示 |
| `--env-file FILE` | 指定写入凭据的 env 文件，默认 `.env` |

参数组合：

```bash
# 写入当前目录的 .env
kaggle b auth -y

# 写入专用文件
kaggle b auth -y --env-file .env.kaggle

# 写入临时文件用于单次任务
kaggle b auth -y --env-file /tmp/cloud-codegen.env
```

生成的主要变量：

```text
MODEL_PROXY_URL
MODEL_PROXY_API_KEY
MODEL_PROXY_EXPIRY_TIME
```

写入 `.env` 不等于导出到当前 shell。如需手动加载：

```bash
set -a
source .env
set +a
```

本项目的 `run_cloud.py local` 会通过 `kllm.TokenManager` 自动申请、缓存并注入凭据，通常
不要求手动 `source .env`。

### 4.2 `kaggle benchmarks init`

初始化本地 Benchmark 开发目录：

```bash
kaggle b init -y
```

参数：

| 参数 | 含义 |
|---|---|
| `-y`, `--yes` | 跳过确认提示 |
| `--env-file FILE` | 凭据和默认模型变量写入位置 |
| `--example-file FILE` | 示例 task 文件路径 |

参数组合：

```bash
# 默认生成 .env、example_task.py 和语法参考
kaggle b init -y

# 自定义 env 文件
kaggle b init -y --env-file .env.kaggle

# 同时自定义示例 task 文件
kaggle b init -y \
  --env-file .env.kaggle \
  --example-file examples/my_task.py
```

本项目已经有 `tasks/repo_task.py`，通常只需要 `auth`，不需要反复执行 `init`。

## 5. 查看可用模型

```bash
kaggle benchmarks tasks models
```

简写：

```bash
kaggle b t models
```

复制命令输出中的模型 slug，用在 `tasks run -m` 或 `run_cloud.py --model`。不要只依赖
项目源码中的静态模型列表，因为 Kaggle 服务端模型可能发生变化。

项目锁定的 `kaggle==2.2.4` 没有 `kaggle benchmarks quota` 子命令。Kaggle 官方主分支文档
可能提前出现该命令，但在本项目版本执行会得到 `invalid choice`。先以本机
`kaggle b --help` 为准；不要为一个尚不可用的命令贸然升级整个环境。

## 6. Dataset 命令

`cloud_codegen` 用私有 Dataset 传输仓库 ZIP 和 `codegen-job.json`。

### 6.1 `kaggle datasets init`

创建 Dataset 元数据模板：

```bash
kaggle datasets init -p ./dataset_staging
```

参数：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `-p`, `--path` | 当前目录 | 创建 `dataset-metadata.json` 的目录 |

最小元数据：

```json
{
  "title": "Cloud Codegen Source",
  "id": "YOUR_KAGGLE_USERNAME/cloud-codegen-source",
  "licenses": [{"name": "CC0-1.0"}]
}
```

`run_cloud.py dataset` 会自动生成该文件，不需要单独执行 `datasets init`。

### 6.2 `kaggle datasets create`

创建一个新 Dataset：

```bash
kaggle datasets create -p ./dataset_staging
```

参数：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `-p`, `--path DIR` | 当前目录 | 包含元数据和上传文件的目录 |
| `-u`, `--public` | 否 | 创建公开 Dataset；不传时默认私有 |
| `-q`, `--quiet` | 否 | 减少进度输出 |
| `-t`, `--keep-tabular` | 否 | 不把表格文件转换为 CSV |
| `-r`, `--dir-mode MODE` | `skip` | 子目录处理方式：`skip`、`zip`、`tar` |
| `--ignore-patterns PATTERN` | 无 | 忽略匹配项，可重复 |

重要组合：

```bash
# 创建默认私有 Dataset，只上传根目录普通文件，跳过子目录
kaggle datasets create -p ./dataset_staging

# 本项目使用：把仓库子目录压缩成 ZIP 后上传
kaggle datasets create \
  -p ./dataset_staging \
  --dir-mode zip

# 安静上传，并避免转换表格文件
kaggle datasets create \
  -p ./dataset_staging \
  --dir-mode zip \
  --keep-tabular \
  --quiet

# 重复忽略规则
kaggle datasets create \
  -p ./dataset_staging \
  --dir-mode zip \
  --ignore-patterns "node_modules/" \
  --ignore-patterns "*.log"
```

不应在代码仓库工作流中使用 `--public`。公开 Dataset 会暴露上传的源码和任务配置。

`dir-mode` 行为：

- `skip`：不上传子目录；
- `zip`：每个子目录作为同名 `.zip` 文件上传；
- `tar`：每个子目录作为同名 `.tar` 文件上传。

这就是 `run_cloud.py` 必须传 `--dir-mode zip`，而 `tasks/repo_task.py` 必须在云端解压仓库的原因。

### 6.3 `kaggle datasets version`

给已有 Dataset 创建新版本：

```bash
kaggle datasets version \
  -p ./dataset_staging \
  -m "update source snapshot"
```

参数：

| 参数 | 是否必需 | 含义 |
|---|---:|---|
| `-p`, `--path DIR` | 否 | 新版本文件和元数据目录，默认当前目录 |
| `-m`, `--message TEXT` | 是 | 版本说明 |
| `-q`, `--quiet` | 否 | 减少输出 |
| `-t`, `--keep-tabular` | 否 | 不转换表格文件 |
| `-r`, `--dir-mode MODE` | 否 | `skip`、`zip` 或 `tar` |
| `-d`, `--delete-old-versions` | 否 | 删除旧版本，具有破坏性 |
| `--ignore-patterns PATTERN` | 否 | 忽略规则，可重复 |

参数组合：

```bash
# 本项目标准更新
kaggle datasets version \
  -p ./dataset_staging \
  -m "update source snapshot" \
  --dir-mode zip

# 安静更新并保留表格原格式
kaggle datasets version \
  -p ./dataset_staging \
  -m "update source snapshot" \
  --dir-mode zip \
  --keep-tabular \
  --quiet
```

不要在没有备份和明确清理需求时使用 `--delete-old-versions`。

### 6.4 Dataset 检查命令

#### 6.4.1 `status`

查看创建或更新状态：

```bash
kaggle datasets status YOUR_KAGGLE_USERNAME/cloud-codegen-example
```

| 参数 | 默认值 | 含义 |
|---|---|---|
| `DATASET` | 必填 | `owner/slug` |
| `--format FORMAT` | 纯文本状态 | `json`，或选择字段的 `json(FIELD,...)` |

结构化输出组合：

```bash
kaggle datasets status YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  --format json

kaggle datasets status YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  --format 'json(status,current_version_number)'
```

#### 6.4.2 `files`

列出当前版本文件：

```bash
kaggle datasets files YOUR_KAGGLE_USERNAME/cloud-codegen-example
```

| 参数 | 默认值 | 含义 |
|---|---|---|
| `DATASET` | 必填 | `owner/slug` |
| `-v`, `--csv` | 关闭 | CSV 输出 |
| `--format FORMAT` | `table` | `table`、`csv` 或 `json` |
| `--page-size NUMBER` | `20` | 每页数量，最大 200 |
| `--page-token TOKEN` | 无 | 继续读取下一页时使用的服务端 token |

`--csv` 和 `--format` 互斥。分页和输出格式可以组合：

```bash
kaggle datasets files YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  --page-size 100 \
  --format json
```

#### 6.4.3 `metadata`

参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `DATASET` | 必填 | `owner/slug` |
| `-p`, `--path PATH` | 当前目录 | 下载或读取 `dataset-metadata.json` 的目录 |
| `--update` | 关闭 | 用本地元数据更新当前远端版本的元数据 |

下载元数据：

```bash
kaggle datasets metadata YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  -p ./downloaded-metadata
```

修改 `./downloaded-metadata/dataset-metadata.json` 后更新远端元数据：

```bash
kaggle datasets metadata YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  -p ./downloaded-metadata \
  --update
```

`metadata --update` 只更新元数据，不上传新的仓库文件；上传源码新版本要用
`datasets version`。

#### 6.4.4 `download`

参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `DATASET` | 必填 | `owner/slug` |
| `-f`, `--file FILE` | 全部文件 | 只下载一个文件，子目录路径会保留 |
| `-p`, `--path PATH` | 当前目录 | 下载目标目录 |
| `-w`, `--wp` | 关闭 | 下载到当前工作目录，相当于路径 `.` |
| `--unzip` | 关闭 | 解压下载包，并删除下载到本地的 ZIP |
| `-o`, `--force` | 关闭 | 跳过本地版本检查并强制覆盖下载 |
| `-q`, `--quiet` | 关闭 | 隐藏下载进度信息 |

下载整个 Dataset 并解压：

```bash
kaggle datasets download YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  -p ./dataset-download \
  --unzip
```

下载单个文件并覆盖本地旧文件：

```bash
kaggle datasets download YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  -f example_repo.zip \
  -p ./dataset-download \
  --force
```

下载到当前目录、安静覆盖并解压：

```bash
kaggle datasets download YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  --wp \
  --unzip \
  --force \
  --quiet
```

`--path` 和 `--wp` 都设置下载目录，语义上应二选一。`--file` 可与 `--path`、`--force`
和 `--quiet` 组合；只下载普通单文件时通常不需要 `--unzip`。

### 6.5 `kaggle datasets delete`

参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `DATASET` | 必填 | 要删除的 `owner/slug` |
| `-y`, `--yes` | 关闭 | 跳过删除确认 |

删除整个 Dataset：

```bash
kaggle datasets delete YOUR_KAGGLE_USERNAME/cloud-codegen-example
```

跳过确认：

```bash
kaggle datasets delete YOUR_KAGGLE_USERNAME/cloud-codegen-example --yes
```

这是破坏性操作，会删除远端 Dataset。不要把它写进默认自动化流程。

## 7. Benchmark Task 命令

### 7.1 Task 名称规则

Task 文件中：

```python
@kbench.task(name="repo-codegen")
```

必须与 push 的 task slug 匹配：

```bash
kaggle b t push repo-codegen -f tasks/repo_task.py
```

创建 task 时使用裸 slug。查看其他公开 task 时可以使用 `owner/task-slug`。

### 7.2 `kaggle benchmarks tasks push`

创建或更新 Benchmark Task：

```bash
kaggle b t push repo-codegen -f tasks/repo_task.py
```

参数：

| 参数 | 是否必需 | 含义 |
|---|---:|---|
| `-f`, `--file FILE` | 是 | 包含 `@kbench.task` 的 Python 文件 |
| `--wait [TIMEOUT]` | 否 | 等待 task 创建完成；可附超时秒数 |
| `--poll-interval SECONDS` | 否 | 最大轮询间隔，默认 60 秒 |
| `-v`, `--verbose` | 否 | 显示详细轮询信息 |
| `-d`, `--kaggle-dataset OWNER/SLUG` | 否 | 挂载 Dataset，可重复 |

参数组合：

```bash
# 推送后立即返回
kaggle b t push repo-codegen -f tasks/repo_task.py

# 等到创建完成，不设置本地超时
kaggle b t push repo-codegen -f tasks/repo_task.py --wait

# 最多等待 600 秒
kaggle b t push repo-codegen \
  -f tasks/repo_task.py \
  --wait 600

# 挂载一个 Dataset
kaggle b t push repo-codegen \
  -f tasks/repo_task.py \
  -d YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  --wait 600

# 挂载多个 Dataset，并显示详细轮询日志
kaggle b t push repo-codegen \
  -f tasks/repo_task.py \
  -d YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  -d YOUR_KAGGLE_USERNAME/shared-fixtures \
  --wait 600 \
  --poll-interval 10 \
  --verbose
```

同一 slug 再次 push 会创建新版本。重新 push 时如果省略所有 `-d`，之前挂载的 Dataset
会被解除，因此每次都要完整重写 Dataset 列表。

### 7.3 `kaggle benchmarks tasks run`

执行已经完成创建的 task：

```bash
kaggle b t run repo-codegen -m anthropic/claude-sonnet-5@default
```

参数：

| 参数 | 含义 |
|---|---|
| `-m`, `--model MODEL` | 指定模型，可重复；省略时进入交互选择 |
| `--wait [TIMEOUT]` | 等待运行完成，可附超时秒数 |
| `--poll-interval SECONDS` | 最大轮询间隔 |
| `-v`, `--verbose` | 显示详细轮询输出 |

参数组合：

```bash
# 交互选择模型
kaggle b t run repo-codegen

# 单模型，提交后立即返回
kaggle b t run repo-codegen \
  -m anthropic/claude-sonnet-5@default

# 单模型并最多等待 900 秒
kaggle b t run repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  --wait 900

# 同一个 task 同时评估多个模型
kaggle b t run repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  -m google/gemini-3-flash-preview \
  --wait 1800 \
  --poll-interval 15 \
  --verbose
```

`--wait` 超时只结束本地等待，不等于取消云端 run。

### 7.4 `kaggle benchmarks tasks list`

参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--name-regex REGEX` | 无 | 按正则表达式筛选 task 名称 |
| `--status STATUS` | 无 | `queued`、`running`、`completed` 或 `errored` |
| `--page-size NUMBER` | `20` | 交互分页器每页显示数量 |
| `--all` | 关闭 | 一次打印所有 task，跳过交互分页器 |

列出自己的 task：

```bash
kaggle b t list
```

筛选名称：

```bash
kaggle b t list --name-regex "codegen"
```

筛选创建状态：

```bash
kaggle b t list --status completed
kaggle b t list --name-regex "codegen" --status errored
```

调整分页大小：

```bash
kaggle b t list --page-size 50
```

非交互脚本中一次输出全部匹配结果：

```bash
kaggle b t list \
  --name-regex "codegen" \
  --status completed \
  --all
```

`--all` 和 `--page-size` 都能被解析，但前者跳过分页器，因此组合使用没有实际价值。

### 7.5 `kaggle benchmarks tasks status`

参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `TASK` | 必填 | 裸 slug 或 `owner/slug` |
| `-m`, `--model MODEL` | 全部模型 | 只显示指定模型，可重复 |

查看 task 和所有模型 run 状态：

```bash
kaggle b t status repo-codegen
```

只看某个模型：

```bash
kaggle b t status repo-codegen \
  -m anthropic/claude-sonnet-5@default
```

同时筛选多个模型：

```bash
kaggle b t status repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  -m google/gemini-3-flash-preview
```

### 7.6 `kaggle benchmarks tasks log`

参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `TASK` | 必填 | 裸 slug 或 `owner/slug` |
| `-m`, `--model MODEL` | 全部 run | 只显示指定模型，可重复 |

查看全部 run 日志：

```bash
kaggle b t log repo-codegen
```

只看指定模型：

```bash
kaggle b t log repo-codegen \
  -m anthropic/claude-sonnet-5@default
```

多个模型：

```bash
kaggle b t log repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  -m google/gemini-3-flash-preview
```

`log` 和 `logs` 是别名。活动 run 会持续输出日志，已完成 run 返回持久化日志。

### 7.7 `kaggle benchmarks tasks download`

下载已完成 run 的输出：

```bash
kaggle b t download repo-codegen
```

参数：

| 参数 | 含义 |
|---|---|
| `-m`, `--model MODEL` | 只下载指定模型，可重复 |
| `-o`, `--output DIR` | 输出目录，默认当前目录 |
| `-s`, `--include-source` | 同时下载运行时 Notebook 源码 |
| `-f`, `--force` | 强制重新下载已缓存的 run |

参数组合：

```bash
# 下载 task 的所有已完成结果
kaggle b t download repo-codegen

# 单模型和指定目录
kaggle b t download repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  -o ./runs/cloud-result

# 多模型
kaggle b t download repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  -m google/gemini-3-flash-preview \
  -o ./runs/model-comparison

# 包含 Notebook 源码
kaggle b t download repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  -o ./runs/cloud-result \
  --include-source

# 覆盖本地缓存，并补下载源码
kaggle b t download repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  -o ./runs/cloud-result \
  --force \
  --include-source
```

CLI 会自动解压 run 输出，并按 task 版本、模型和 run ID 建立多级目录。已有目录默认视为缓存；
要重新下载必须使用 `--force`。

### 7.8 `delete` 与 `publish`

`delete` 参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `TASK` | 必填 | 要删除的 task slug |
| `-y`, `--yes` | 关闭 | 跳过确认 |

CLI 提供 task 删除命令：

```bash
kaggle b t delete repo-codegen
kaggle b t delete repo-codegen --yes
```

但当前 Kaggle 服务端尚不支持真正删除 Benchmark Task，因此不要依赖它清理测试版本。

`publish` 参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `TASK` | 必填 | 自己拥有的 task slug |
| `--no-publish-backing-notebook` | 关闭 | 只发布 task，不发布背后的 Notebook |

发布 task：

```bash
kaggle b t publish repo-codegen
```

只发布 task、不发布 backing notebook：

```bash
kaggle b t publish repo-codegen --no-publish-backing-notebook
```

`cloud_codegen` 上传的是私有源码，不应执行 publish。当前 CLI 也不提供 unpublish 工作流。

## 8. 本项目原生命令完整流程

### 8.1 首次认证

```bash
kaggle auth login
kaggle config view
kaggle b t models
```

### 8.2 创建私有代码 Dataset

```bash
kaggle datasets create \
  -p /tmp/cloud-codegen-example-dataset \
  --dir-mode zip

kaggle datasets status \
  YOUR_KAGGLE_USERNAME/cloud-codegen-example

kaggle datasets files \
  YOUR_KAGGLE_USERNAME/cloud-codegen-example
```

### 8.3 推送、运行、查看与下载

```bash
kaggle b t push repo-codegen \
  -f ./tasks/repo_task.py \
  -d YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  --wait 600

kaggle b t run repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  --wait 900

kaggle b t status repo-codegen

kaggle b t log repo-codegen \
  -m anthropic/claude-sonnet-5@default

kaggle b t download repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  -o ./runs/cloud-result
```

## 9. 原生 Kaggle CLI 与 `run_cloud.py` 的差异

| 能力 | 原生 Kaggle CLI | `run_cloud.py` |
|---|---|---|
| 生成 Dataset 元数据 | 需要 `datasets init` 或手写 | 自动生成 |
| 仓库打包 | 手动 staging 和 `--dir-mode zip` | 自动复制和 ZIP 上传 |
| 传递提示词 | Kaggle CLI 没有 prompt 参数 | `local/dataset -p` 或 `--prompt-file` |
| Task slug | 手动输入 | 由 `-t repo/spec/codegen` 映射 |
| 多模型 run | `-m` 可重复 | 当前一次只能传一个 `--model` |
| push 等待 | 可省略、无限等待或设超时 | 固定最多等待 600 秒 |
| run 等待 | 可省略、无限等待或设超时 | `--wait` 必须是整数，默认 900 秒 |
| 下载源码 | 支持 `--include-source` | 当前未暴露 |
| 强制重下 | 支持 `--force` | 当前未暴露 |
| 应用代码补丁 | 不支持 | 当前也不自动应用，只生成 diff |

`run_cloud.py` 的 `-p` 是提示词，不是 Kaggle CLI 常见的 `-p/--path`。两者属于不同命令，
不要混用。

## 10. 故障排查顺序

```bash
# 1. 版本与认证
kaggle --version
kaggle config view

# 2. 模型是否存在
kaggle b t models

# 3. Dataset 是否创建完成并含有仓库 ZIP
kaggle datasets status YOUR_KAGGLE_USERNAME/cloud-codegen-example
kaggle datasets files YOUR_KAGGLE_USERNAME/cloud-codegen-example

# 4. Task 是否创建完成
kaggle b t status repo-codegen

# 5. 查看模型运行日志
kaggle b t log repo-codegen -m anthropic/claude-sonnet-5@default

# 6. 下载输出
kaggle b t download repo-codegen \
  -m anthropic/claude-sonnet-5@default \
  -o ./runs/cloud-result
```

任何上传、push、run 或删除命令失败后，都先检查退出码和错误信息，不要直接反复执行。
Dataset `create` 重试可能遇到 slug 已存在，此时应确认远端状态，再决定使用 `version`。
