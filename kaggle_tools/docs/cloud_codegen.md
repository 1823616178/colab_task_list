# Cloud Codegen 启动手册

这个目录用于把一个本地代码仓库作为私有 Kaggle Dataset 上传，让 Kaggle
Benchmarks 中的模型在云端副本里查看、修改和测试代码，再把改动下载到本地。

它目前是一个**批处理工作流**，不是实时双向同步：

```text
本地仓库 -> 私有 Dataset -> Benchmark Task -> 云端工作副本
         <- changes.diff + verification.txt + workspace <-
```

模型不会直接修改本地仓库。下载后必须先审查 `changes.diff`，再由你手动应用。

Kaggle 原生命令、参数和组合示例见 [KAGGLE_CLI.md](docs/KAGGLE_CLI.md)。本文后半部分专门
说明本项目封装脚本 `run_cloud.py` 的全部用法。

## 1. 当前适用范围

当前最完整的流程是 `repo` 任务：

- 上传一个代码仓库；
- 模型通过 `list_files`、`read_file`、`write_file` 查看和修改云端副本；
- 模型可以反复执行一个固定的验收命令；
- 云端输出完整工作副本、文本 diff 和验收报告；
- 原始本地仓库不会被任务直接修改。

另外两个任务暂时只作为示例：

- `codegen`：生成一个固定的区间合并模块；
- `spec`：读取固定 API 契约，生成一个客户端模块。

日常使用自己的仓库时，只使用 `-t repo`。

### 目录结构

```text
cloud_codegen/
├── run_cloud.py                  统一命令入口
├── requirements.txt             本项目的完整 Python 依赖
├── tasks/                        可单文件 push 的 Benchmark Task
│   ├── repo_task.py              仓库修改任务
│   ├── spec_task.py              API 契约示例任务
│   └── codegen_task.py           固定代码生成示例任务
├── examples/
│   ├── example_repo/             故意带失败测试的示例仓库
│   ├── prompts/                  可直接使用的提示词示例
│   └── spec/usage-api.md         spec 任务的示例契约
├── tests/                        不调用模型的本地回归测试
├── docs/KAGGLE_CLI.md            Kaggle 原生命令参考
└── runs/                         本地与云端产物，已被 Git 忽略
```

`tasks/` 中的文件必须保持单文件自包含，因为 Kaggle push 时只上传选中的任务文件。
`runs/`、Dataset staging、`*.task.json` 和 `*.run.json` 都是生成物，不属于源码。

## 2. 准备环境

建议使用 Python 3.11 或更高版本。在本目录执行：

```bash
cd /backup/code/kaggle-code/kaggle/cloud_codegen

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

确认命令可用：

```bash
kaggle --version
python run_cloud.py --help
python -m pytest -q tests
```

### Kaggle 认证

优先使用 Kaggle OAuth：

```bash
kaggle auth login
kaggle config view
```

也可以使用 `~/.kaggle/access_token` 或旧版 `~/.kaggle/kaggle.json`。
认证文件不要放进准备上传的代码仓库。

查看当前账号可用的 Benchmark 模型：

```bash
kaggle b t models
```

可选：先验证模型代理凭据和模型调用。这个检查会消耗少量 AI 额度：

```bash
PYTHONPATH=.. python -m kllm.cli doctor
```

## 3. 先跑通内置示例

第一次不要直接上传正式项目。先用 `examples/example_repo` 验证完整链路。

下面所有命令都从 `cloud_codegen` 目录执行。把示例里的
`YOUR_KAGGLE_USERNAME` 换成 `kaggle config view` 显示的用户名。

### 第一步：本地试跑

```bash
python run_cloud.py -t repo local \
  --repo ./examples/example_repo \
  -p "修复 textstats 包，让全部测试通过；不要修改 tests/" \
  --output ./runs/local-example \
  --model anthropic/claude-sonnet-5@default
```

这一步的代码和测试都在本机执行，但模型请求会使用 Kaggle AI 额度。成功后重点查看：

```text
runs/local-example/changes.diff
runs/local-example/verification.txt
runs/local-example/workspace/
```

短提示词使用 `-p/--prompt`。多行或包含引号的需求建议写进 UTF-8 文件：

```bash
python run_cloud.py -t repo local \
  --repo ./examples/example_repo \
  --prompt-file /absolute/path/to/task.md \
  --output ./runs/local-example \
  --model anthropic/claude-sonnet-5@default
```

`--prompt` 和 `--prompt-file` 不能同时使用。两者都不传时，任务回退到
`tasks/repo_task.py` 中的 `SPEC`。

必须先让本地试跑成功，再进行 push。Benchmark Task 的服务端删除接口目前不可用，
反复 push 会不断产生永久任务版本。

### 第二步：创建私有代码数据集

`--staging` 指向的目录如果已存在会被整个删除，因此只能使用专门的临时目录。脚本会拒绝
与仓库相同、位于仓库内部或包含仓库的 staging 路径；默认使用 `runs/dataset-staging`。

```bash
python run_cloud.py -t repo dataset \
  --repo ./examples/example_repo \
  -p "修复 textstats 包，让全部测试通过；不要修改 tests/" \
  --owner YOUR_KAGGLE_USERNAME \
  --slug cloud-codegen-example \
  --staging /tmp/cloud-codegen-example-dataset
```

脚本会：

1. 复制仓库到 staging；
2. 忽略 `.git`、Python 缓存和测试缓存；
3. 让 Kaggle CLI 把仓库子目录上传为 ZIP；
4. 把提示词与运行期配置写入 `codegen-job.json`（`repo_name` 总会写入）；
5. 创建默认私有的 Dataset。

出现确认提示时，在另一个终端检查 staging 内容；也可以先输入 `n` 取消，检查后再运行一次：

```bash
find /tmp/cloud-codegen-example-dataset -maxdepth 4 -type f -print
```

确认没有 `.env`、私钥、访问令牌、生产配置或不应上传的数据后再输入 `y`。

### 第三步：推送 Benchmark Task

```bash
python run_cloud.py -t repo push \
  --dataset YOUR_KAGGLE_USERNAME/cloud-codegen-example
```

确认输出里的任务是 `repo-codegen`，数据集名称正确，然后输入 `y`。

注意：以后每次重新 push 都必须再次传 `--dataset`。省略它会解除之前的数据集挂载。

云端 `run` 命令本身不能接收 `-p`、`--verify` 这类配置。云端读的是 Dataset 中的
`codegen-job.json`，所以 `local` 与 `dataset` 应传入相同的提示词和相同的
`--verify` / `--protected` / `--max-rounds`，否则本地跑通的配置不会带到云端。

### 第四步：云端运行

```bash
python run_cloud.py -t repo run \
  --model anthropic/claude-sonnet-5@default \
  --wait 900
```

`--wait` 单位为秒。超时只表示本地停止等待，不一定表示云端任务失败。

### 第五步：查看状态和日志

```bash
python run_cloud.py -t repo status

python run_cloud.py -t repo log \
  --model anthropic/claude-sonnet-5@default
```

如果状态仍是运行中，稍后重新执行 `status`。如果任务失败，先从 `log` 中检查：

- 是否挂载了正确 Dataset；
- 是否找到了 `example_repo.zip`；
- 验收命令所需依赖是否存在；
- 模型是否用尽工具调用轮数。

### 第六步：下载结果

每次排查时使用一个新的输出目录，避免把不同 run 的结果混在一起：

```bash
python run_cloud.py -t repo download \
  --model anthropic/claude-sonnet-5@default \
  --output ./runs/cloud-example-01
```

Kaggle CLI 会把结果组织成多级目录。查找关键文件：

```bash
find ./runs/cloud-example-01 -name changes.diff -type f -print
find ./runs/cloud-example-01 -name verification.txt -type f -print
```

一个 run 的主要产物是：

```text
changes.diff       模型产生的文本改动
verification.txt  最终验收命令、退出码和输出
workspace/         修改后的完整仓库副本
*.run.json         Benchmark 对话、断言和用量记录
```

下载命令可能同时拿到这个模型以前已完成的 run。不要仅凭目录排序判断结果，
应同时核对任务版本、运行时间、`verification.txt` 和 diff 内容。

## 4. 接入自己的仓库

假设本地仓库为：

```text
/absolute/path/my-project
```

### 第一步：确定任务配置

`tasks/repo_task.py` 顶部的 `DEFAULT_*` 只是兜底值，**不需要为每个仓库去改源码**
（云端任务一旦 push 就删不掉，改一次配置就多留一条永久记录）。四项配置都能在运行时
覆盖，本地走命令行参数，云端走 Dataset 里的 `codegen-job.json`：

| 覆盖项 | `local` / `dataset` 参数 | 默认值 | 说明 |
|---|---|---|---|
| 提示词 | `-p` / `--prompt-file` | 内置 `DEFAULT_SPEC` | 交给编码 agent 的真实需求，写清范围和验收标准 |
| 验收命令 | `--verify` | `python -m pytest -q` | 一行 shell 风格字符串，按 `shlex` 拆成参数列表；不支持管道和重定向，需要就写进脚本再调脚本 |
| 受保护路径 | `--protected` | `tests/,pytest.ini` | 逗号分隔的路径前缀；传空串 `""` 表示全部可写，仓库要从零生成时才这么做 |
| 工具轮数上限 | `--max-rounds` | `16` | 只够改几个文件；仓库越大越要往上调 |
| 仓库目录名 | 不用传 | `example_repo` | `dataset` 会自动把实际上传的目录名写进 `codegen-job.json`，云端据此在 `/kaggle/input` 下定位 |

配置规则：

- `--protected` 至少保护测试、密钥、生成基线和不能修改的配置，否则模型可以改测试蒙混过关；
- 放开 `--protected` 不会放开路径越界防护，工作区外始终写不进去；
- 目前每个文件最多读取 60 KB，超大文件不会完整提供给模型；
- 当前 runner 不会自动安装项目依赖。

本地想临时换一项而不重新上传 Dataset，可以直接设环境变量：`CODEGEN_PROMPT`、
`CODEGEN_VERIFY`、`CODEGEN_PROTECTED`、`CODEGEN_MAX_ROUNDS`、`CODEGEN_REPO_NAME`。
环境变量优先级高于 `codegen-job.json`。

如果项目依赖不在 Kaggle 默认镜像里，先不要直接云端运行。当前版本尚未提供受控的
`SETUP_COMMAND`，也没有保证云端可以联网安装依赖。第一阶段建议选择依赖已经存在、
验收命令简单的 Python 项目。

### 第二步：本地试跑真实仓库

```bash
python run_cloud.py -t repo local \
  --repo /absolute/path/my-project \
  --prompt-file /absolute/path/to/task.md \
  --output ./runs/my-project-local-01 \
  --model anthropic/claude-sonnet-5@default
```

确认以下条件后才能上传：

- 原始仓库没有被修改；
- `workspace/` 中的改动符合预期；
- `verification.txt` 的 `exit_code=0`；
- `changes.diff` 没有修改受保护文件。

### 多次修改提示词

一次 `local` 内部允许模型反复读取、修改和运行验收命令，但不同的 `local` 调用之间没有
对话记忆。每个提示词版本都应包含完整目标、约束和验收条件：

```text
/absolute/path/prompts/
├── feature-v1.md
├── feature-v2.md
└── feature-v3.md
```

使用独立输出目录比较不同版本：

```bash
python run_cloud.py -t repo local \
  --repo /absolute/path/my-project \
  --prompt-file /absolute/path/prompts/feature-v2.md \
  --output ./runs/my-project-v2
```

如果 v2 要接着 v1 生成的代码继续，先人工审查并把 v1 的 `changes.diff` 应用到真实仓库，
再启动 v2。云端更换提示词时，使用同一个 Dataset slug 执行 `dataset --update`，随后重新
`push`；只更换模型或重复 `run` 不需要重新 push。

### 第三步：上传或更新数据集

首次创建：

```bash
python run_cloud.py -t repo dataset \
  --repo /absolute/path/my-project \
  --prompt-file /absolute/path/to/task.md \
  --owner YOUR_KAGGLE_USERNAME \
  --slug cloud-codegen-my-project \
  --staging /tmp/cloud-codegen-my-project-dataset
```

本地仓库有新版本后，更新同一个 Dataset：

```bash
python run_cloud.py -t repo dataset \
  --repo /absolute/path/my-project \
  --prompt-file /absolute/path/to/task.md \
  --owner YOUR_KAGGLE_USERNAME \
  --slug cloud-codegen-my-project \
  --staging /tmp/cloud-codegen-my-project-dataset \
  --update \
  --message "update source snapshot"
```

### 第四步：重新 push 并运行

任务源码、`SPEC`、验收命令或 Dataset 发生变化后，都重新 push，并再次指定 Dataset：

```bash
python run_cloud.py -t repo push \
  --dataset YOUR_KAGGLE_USERNAME/cloud-codegen-my-project

python run_cloud.py -t repo run \
  --model anthropic/claude-sonnet-5@default \
  --wait 900
```

然后按前面的 `status`、`log`、`download` 步骤取回结果。

## 5. 把改动应用回本地

当前版本不会自动覆盖本地文件，这是有意保留的安全边界。

先确认本地仓库从上传以后没有发生新的修改：

```bash
git -C /absolute/path/my-project status --short
```

阅读云端报告和补丁：

```bash
sed -n '1,240p' /absolute/path/to/verification.txt
sed -n '1,320p' /absolute/path/to/changes.diff
```

对独立 Git 仓库，先检查补丁能否干净应用：

```bash
git -C /absolute/path/my-project apply --check /absolute/path/to/changes.diff
```

检查通过后再应用：

```bash
git -C /absolute/path/my-project apply /absolute/path/to/changes.diff
```

随后必须在本地重新验收：

```bash
cd /absolute/path/my-project
python -m pytest -q
git diff --check
git diff
```

当前 `changes.diff` 只可靠覆盖 UTF-8 文本内容。二进制文件、文件权限变化，以及某些
新增或删除文件场景可能无法通过 diff 完整表达。遇到 `git apply --check` 失败时不要强行
覆盖；对照下载结果里的 `workspace/` 手动审查相应文件。

### 失败后代码在哪里

`local` 的产物**始终**落在 `-o` 指定的目录里，通过与否都写：

```text
runs/<name>/
├── workspace/          模型改完的完整代码树，直接能看能跑
├── changes.diff        相对原始仓库的累计 diff
├── verification.txt    验收命令、退出码和完整输出
└── *.run.json          kbench 的运行记录，含完整对话
```

原始仓库不会被改动，要采纳得自己 `git apply changes.diff` 或从 `workspace/` 拷回去。

云端 run 的这几样产物在 `/kaggle/working` 下，用 `download` 取回本地：

```bash
python run_cloud.py -t repo download -m <model> -o ./runs/cloud-01
```

**重跑会覆盖 `workspace/`。** 不加 `--resume` 时，任务启动第一件事就是删掉旧工作副本
再从原始仓库重新复制——上一轮的成果在产出任何新东西之前就没了。想在上一轮基础上
接着改，加 `--resume`；想留档，换一个 `-o` 目录。

```bash
# 接着上一轮改，验收命令和轮数照旧要给
python run_cloud.py -t repo local --repo ./examples/psv_quota \
  --prompt-file ./next-step.md --output ./runs/psv-quota-local-01 --resume \
  --verify "bash scripts/test.sh" --protected "" --max-rounds 120
```

`--resume` 只影响工作副本，`changes.diff` 的基准始终是原始仓库，所以多轮下来 diff 是
累计的，不是增量的。云端没有这个参数：每次 run 都是全新容器，只能靠 Dataset 传入上一
轮的成果。

### 运行产物管理

默认本地输出、Dataset staging 和云端下载分别位于：

```text
runs/local/
runs/dataset-staging/
runs/cloud/
```

每次正式运行建议显式指定带版本的目录，例如 `runs/my-project-v3`。这些目录已被
`.gitignore` 忽略，可以按需清理；需要长期保留的结果应移出 `runs/` 或归档到其他位置。

## 6. 上传前安全检查

Kaggle Dataset 默认是私有的，但代码仍然会离开本机并保存在第三方平台。当前打包逻辑
不会读取项目自己的 `.gitignore`，只排除少量缓存目录，因此每次上传前都要主动检查。

不要上传：

- `.env`、`kaggle.json`、`access_token`；
- SSH 私钥、云服务密钥、数据库口令；
- 客户数据、生产日志、未脱敏样本；
- `node_modules`、虚拟环境、大型构建产物；
- 指向仓库外部敏感位置的符号链接。

更稳妥的做法是先制作一个专门用于上传的干净代码副本，再把这个副本传给 `--repo`。
副本目录名可以随意，`dataset` 会把它写进 `codegen-job.json` 的 `repo_name`。

永远不要执行 `kaggle b t publish`。发布操作会把任务从私有改成公开。

## 7. 常见问题

### `kaggle: command not found`

确认虚拟环境已激活，并重新安装依赖：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 本地任务提示缺少模型代理凭据

```bash
kaggle b auth -y
```

然后重新执行 `local`。如果仍失败，运行 `python -m kllm.cli doctor` 查看认证、地区限制
和模型代理响应。

### 云端提示找不到仓库

依次检查：

1. Dataset 里 `codegen-job.json` 的 `repo_name` 是否等于上传的仓库目录名；
2. Dataset 文件列表里是否存在 `<repo_name>.zip`；
3. push 是否传了正确的 `--dataset owner/slug`；
4. 更新 Dataset 后是否重新 push 任务；
5. 日志中的 `/kaggle/input` 是否挂载成功。

### 云端验收提示缺少 Python 包

当前 runner 不负责安装业务依赖。先换成 Kaggle 镜像已有的依赖，或者在后续版本中实现
经过白名单约束的 setup 阶段，不要把任意 shell 安装命令直接暴露给模型。

### 下载目录里有多个结果

`download` 会下载指定模型所有可用的已完成 run。使用新的输出目录，并根据任务版本、
运行时间、验收报告和 diff 内容选择本次结果。当前版本还没有 `job_id` 精确关联机制。

### 能否删除 Benchmark Task

Kaggle CLI 有 delete 命令外壳，但服务端目前不支持删除任务及历史 run。先本地试跑，
确认任务配置稳定后再 push。

## 8. 每次运行检查表

```text
[ ] 提示词、--verify、--protected、--max-rounds 都符合这个仓库
[ ] local 与 dataset 使用了同一份提示词和同一组配置
[ ] 本地 local 运行通过
[ ] staging 中没有密钥、个人数据和大型无关文件
[ ] Dataset 创建或更新成功
[ ] push 时重新指定了 Dataset
[ ] 云端 run 完成，verification.txt 的 exit_code 为 0
[ ] changes.diff 已人工审查
[ ] git apply --check 通过
[ ] 应用后本地测试再次通过
```

完成这套流程后，你得到的是可审查的“云端生成补丁”工作流。自动 job ID、冲突检测和
一键安全应用尚未实现，在这些能力完成前，不要把下载的 `workspace/` 直接覆盖本地仓库。

## 9. `run_cloud.py` 完整命令参考

### 9.1 命令结构和参数位置

统一格式：

```bash
python run_cloud.py [-t TASK] COMMAND [COMMAND_OPTIONS]
```

`TASK` 可取 `repo`、`spec`、`codegen`，默认是 `repo`。`COMMAND` 可取 `local`、
`dataset`、`push`、`run`、`status`、`log`、`download`。

`-t/--task` 是全局参数，必须写在子命令前面：

```bash
# 正确
python run_cloud.py -t repo local --repo ./examples/example_repo

# 也正确：repo 是默认任务
python run_cloud.py local --repo ./examples/example_repo

# 错误：local 后面只接受 local 自己的参数
python run_cloud.py local -t repo --repo ./examples/example_repo
```

三个任务的映射如下：

| `-t/--task` | Kaggle Task slug | 执行文件 | 用途 |
|---|---|---|---|
| `repo` | `repo-codegen` | `tasks/repo_task.py` | 修改一个已有仓库，主要工作流 |
| `spec` | `spec-codegen` | `tasks/spec_task.py` | 根据 `usage-api.md` 生成客户端，示例任务 |
| `codegen` | `cloud-codegen` | `tasks/codegen_task.py` | 生成固定区间合并模块，示例任务 |

不确定参数属于哪一级时直接查看帮助：

```bash
python run_cloud.py --help
python run_cloud.py local --help
python run_cloud.py dataset --help
python run_cloud.py push --help
python run_cloud.py run --help
python run_cloud.py status --help
python run_cloud.py log --help
python run_cloud.py download --help
```

### 9.2 `local`：本地试跑

语法：

```bash
python run_cloud.py [-t TASK] local \
  [-m MODEL] [-r REPO] [-s SPEC] [-o OUTPUT] \
  [-p PROMPT | --prompt-file PROMPT_FILE]
```

参数：

| 参数 | 默认值 | 适用任务 | 说明 |
|---|---|---|---|
| `-m`, `--model` | 环境变量 `LLM_DEFAULT`，未设置时为项目默认模型 | 全部 | 指定本次本地试跑使用的模型 |
| `-r`, `--repo` | 无 | `repo` | 本地仓库目录；`repo` 任务通常必须传 |
| `-s`, `--spec` | 无 | `spec` | 本地契约文档；设置运行时 `SPEC_PATH` |
| `-o`, `--output` | `./runs/local` | 全部 | 工作目录和输出目录，自动创建 |
| `--resume` | 关闭 | 仅 `repo` | 接着 `-o` 目录里上一轮的 `workspace/` 改，而不是从原始仓库重新复制 |
| `-p`, `--prompt` | 内置 `DEFAULT_SPEC` | 仅 `repo` | 直接传一段非空提示词 |
| `--prompt-file` | 内置 `DEFAULT_SPEC` | 仅 `repo` | 从 UTF-8 文件读取多行提示词 |
| `--verify` | `python -m pytest -q` | 仅 `repo` | 验收命令，一行 shell 风格字符串 |
| `--protected` | `tests/,pytest.ini` | 仅 `repo` | 不许模型修改的路径前缀，逗号分隔；`""` 表示不保护 |
| `--max-rounds` | `16` | 仅 `repo` | 工具调用轮数上限 |

`-p` 和 `--prompt-file` 互斥，不能同时出现。提示词参数传给 `spec` 或 `codegen` 会在
启动任务前报错。`-r` 和 `-s` 虽能被解析器接受，但应只按上表用于对应任务。

`repo` 使用内置提示词：

```bash
python run_cloud.py -t repo local \
  --repo ./examples/example_repo \
  --output ./runs/local-example
```

`repo` 使用短提示词：

```bash
python run_cloud.py -t repo local \
  --repo ./examples/example_repo \
  -p "修复失败测试，只改实现，不修改 tests 目录" \
  --output ./runs/local-fix
```

`repo` 使用多行提示词文件：

```bash
python run_cloud.py -t repo local \
  --repo /absolute/path/to/my_repo \
  --prompt-file ./examples/prompts/fix-textstats.md \
  --output ./runs/local-my-repo
```

指定模型：

```bash
python run_cloud.py -t repo local \
  --repo ./examples/example_repo \
  --model anthropic/claude-sonnet-5@default \
  --output ./runs/local-sonnet
```

`spec` 任务：

```bash
python run_cloud.py -t spec local \
  --spec ./examples/spec/usage-api.md \
  --model anthropic/claude-sonnet-5@default \
  --output ./runs/local-spec
```

`codegen` 固定示例不需要仓库或契约：

```bash
python run_cloud.py -t codegen local \
  --output ./runs/local-codegen
```

`local` 使用本机 CPU、内存和 Python 环境，但模型请求仍通过 Kaggle Model Proxy，因而会
消耗对应模型额度。脚本从 Kaggle/kllm 凭据存储动态取得 token；通常不需要手动设置
`MODEL_PROXY_API_KEY`。

### 9.3 `dataset`：创建或更新私有代码 Dataset

语法：

```bash
python run_cloud.py [-t TASK] dataset \
  -r REPO [--slug SLUG] [--owner OWNER] [--staging DIRECTORY] \
  [--update] [-m MESSAGE] [-y] \
  [-p PROMPT | --prompt-file PROMPT_FILE]
```

参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-r`, `--repo` | 必填 | 要复制和上传的本地仓库目录 |
| `--slug` | 仓库目录名，并把 `_` 换成 `-` | Dataset slug |
| `--owner` | 从 `kaggle config view` 读取 | Kaggle 用户名或组织名 |
| `--staging` | `./runs/dataset-staging` | 临时打包目录；每次运行会先完整删除并重建 |
| `--update` | 关闭 | 使用 `datasets version` 更新已有 Dataset |
| `-m`, `--message` | `update` | `--update` 时的版本说明；首次创建不会使用 |
| `-y`, `--yes` | 关闭 | 跳过上传确认，适合已审查内容的脚本化运行 |
| `-p`, `--prompt` | 内置 `DEFAULT_SPEC` | 把短提示词写入 Dataset 的 `codegen-job.json` |
| `--prompt-file` | 内置 `DEFAULT_SPEC` | 把 UTF-8 多行提示词写入 `codegen-job.json` |
| `--verify` | `python -m pytest -q` | 把验收命令写入 `codegen-job.json` |
| `--protected` | `tests/,pytest.ini` | 把受保护路径前缀写入 `codegen-job.json` |
| `--max-rounds` | `16` | 把工具轮数上限写入 `codegen-job.json` |

脚本会忽略仓库中的 `.git`、`__pycache__`、`.pytest_cache` 和 `*.pyc`，生成
`dataset-metadata.json`，然后通过 `--dir-mode zip` 上传仓库目录。Dataset 元数据当前固定
使用 `CC0-1.0`，且 `kaggle datasets create` 默认创建私有 Dataset。

首次创建，自动推断 owner 和 slug：

```bash
python run_cloud.py -t repo dataset \
  --repo ./examples/example_repo
```

首次创建，显式指定全部关键参数：

```bash
python run_cloud.py -t repo dataset \
  --repo /absolute/path/to/my_repo \
  --owner YOUR_KAGGLE_USERNAME \
  --slug my-repo-codegen \
  --staging ./runs/dataset-staging \
  --prompt-file ./examples/prompts/fix-textstats.md
```

非交互首次创建：

```bash
python run_cloud.py -t repo dataset \
  --repo ./examples/example_repo \
  --owner YOUR_KAGGLE_USERNAME \
  --slug cloud-codegen-example \
  -p "修复全部失败测试" \
  --yes
```

更新已有 Dataset，并写版本说明：

```bash
python run_cloud.py -t repo dataset \
  --repo ./examples/example_repo \
  --owner YOUR_KAGGLE_USERNAME \
  --slug cloud-codegen-example \
  --prompt-file ./examples/prompts/fix-textstats.md \
  --update \
  --message "refresh source and task prompt"
```

组合规则：

- 首次创建不要传 `--update`；同一 `owner/slug` 已存在时改用 `--update`。
- `--message` 只影响 `--update`，与首次创建组合不会生效。
- `-p` 和 `--prompt-file` 只能二选一；都不传时云端 `tasks/repo_task.py` 使用内置 `SPEC`。
- 提示词只由 `repo` 任务读取，所以不要给 `-t spec` 或 `-t codegen` 传提示词参数。
- `--staging` 是破坏性临时目录，不能指向仓库本身、仓库父目录或存放重要文件的目录。
- `-y` 只跳过本地确认，不改变 Kaggle 的权限、可见性或上传结果。

### 9.4 `push`：创建或更新 Benchmark Task

语法：

```bash
python run_cloud.py [-t TASK] push [-d OWNER/SLUG ...] [-y]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-d`, `--dataset` | 无 | 挂载 Dataset，参数可重复 |
| `-y`, `--yes` | 关闭 | 跳过不可撤销操作的本地确认 |

脚本固定把对应任务源文件推送到对应 slug，并固定等待最多 600 秒。Kaggle 当前不支持删除
已推送的 Benchmark Task 及历史 run，因此应先执行 `local`。

推送 `repo` 任务并挂载一个代码 Dataset：

```bash
python run_cloud.py -t repo push \
  --dataset YOUR_KAGGLE_USERNAME/cloud-codegen-example
```

挂载多个 Dataset：

```bash
python run_cloud.py -t repo push \
  -d YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  -d YOUR_KAGGLE_USERNAME/shared-fixtures
```

非交互推送：

```bash
python run_cloud.py -t repo push \
  -d YOUR_KAGGLE_USERNAME/cloud-codegen-example \
  --yes
```

无 Dataset 的固定 `codegen` 示例：

```bash
python run_cloud.py -t codegen push
```

`repo` 不传 `-d` 通常会使云端找不到仓库。再次 push 时仍要重新写出所有需要挂载的
`-d`；不要假设脚本会从前一次 push 自动继承挂载列表。

### 9.5 `run`：提交云端模型运行

语法：

```bash
python run_cloud.py [-t TASK] run [-m MODEL] [--wait SECONDS]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-m`, `--model` | `anthropic/claude-sonnet-5@default` | 本次运行使用的模型 |
| `--wait` | `900` | 最多等待的秒数；`0` 表示提交后立即返回 |

默认模型、默认等待 900 秒：

```bash
python run_cloud.py -t repo run
```

指定模型：

```bash
python run_cloud.py -t repo run \
  --model anthropic/claude-sonnet-5@default
```

提交后立即返回，再用 `status` 查看：

```bash
python run_cloud.py -t repo run \
  --model anthropic/claude-sonnet-5@default \
  --wait 0

python run_cloud.py -t repo status
```

给长任务更长等待时间：

```bash
python run_cloud.py -t repo run \
  --model anthropic/claude-sonnet-5@default \
  --wait 1800
```

`--wait` 只控制本地 CLI 等待，不是模型执行时限。脚本当前一次只接收一个 `--model`；若要
一次提交多个模型，使用 [KAGGLE_CLI.md](docs/KAGGLE_CLI.md) 中的原生命令。

### 9.6 `status`：查看 Task 和 run 状态

语法：

```bash
python run_cloud.py [-t TASK] status
```

这个子命令没有自己的参数，只根据 `-t` 查询对应 slug：

```bash
python run_cloud.py -t repo status
python run_cloud.py -t spec status
python run_cloud.py -t codegen status
```

不要给 `status` 添加 `--model`；状态输出本身会列出各模型的 run。

### 9.7 `log`：查看指定模型日志

语法：

```bash
python run_cloud.py [-t TASK] log [-m MODEL]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-m`, `--model` | `anthropic/claude-sonnet-5@default` | 要查看日志的模型 |

```bash
# 默认模型
python run_cloud.py -t repo log

# 显式模型
python run_cloud.py -t repo log \
  --model anthropic/claude-sonnet-5@default
```

当前封装没有 run ID 参数；同一模型有多次运行时，Kaggle CLI 决定返回哪次日志。需要更细
控制时查看原生命令帮助：`kaggle b t log --help`。

### 9.8 `download`：下载并解压产物

语法：

```bash
python run_cloud.py [-t TASK] download [-m MODEL] [-o OUTPUT]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-m`, `--model` | `anthropic/claude-sonnet-5@default` | 下载该模型的已完成结果 |
| `-o`, `--output` | `./runs/cloud` | 下载目录；脚本会转成绝对路径 |

```bash
# 使用全部默认值
python run_cloud.py -t repo download

# 推荐：模型和本次输出目录都写清楚
python run_cloud.py -t repo download \
  --model anthropic/claude-sonnet-5@default \
  --output ./runs/cloud-example
```

成功后脚本会递归打印下载目录中的文件。`repo` 任务重点检查：

```text
changes.diff
verification.txt
workspace/
```

该封装没有暴露原生命令的 `--force` 和 `--include-source`。同一模型存在多个已完成 run
时可能下载多个结果，使用全新的输出目录并结合日志、验收报告和时间识别目标结果。

### 9.9 参数兼容矩阵

`是` 表示该参数由子命令定义；`有效范围` 进一步说明业务上是否真正生效。

| 参数 | local | dataset | push | run | status | log | download | 有效范围 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `-t/--task` | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 全局，必须位于子命令前 |
| `-m/--model` | 是 | 否 | 否 | 是 | 否 | 是 | 是 | `local/run/log/download` |
| `-r/--repo` | 是 | 是 | 否 | 否 | 否 | 否 | 否 | `repo local`、所有 `dataset` |
| `-s/--spec` | 是 | 否 | 否 | 否 | 否 | 否 | 否 | 仅 `spec local` |
| `-o/--output` | 是 | 否 | 否 | 否 | 否 | 否 | 是 | 本地工作目录或下载目录 |
| `-p/--prompt` | 是 | 是 | 否 | 否 | 否 | 否 | 否 | 仅 `repo local/dataset` |
| `--prompt-file` | 是 | 是 | 否 | 否 | 否 | 否 | 否 | 仅 `repo local/dataset`，与 `-p` 互斥 |
| `--verify` | 是 | 是 | 否 | 否 | 否 | 否 | 否 | 仅 `repo local/dataset` |
| `--protected` | 是 | 是 | 否 | 否 | 否 | 否 | 否 | 仅 `repo local/dataset` |
| `--max-rounds` | 是 | 是 | 否 | 否 | 否 | 否 | 否 | 仅 `repo local/dataset` |
| `--resume` | 是 | 否 | 否 | 否 | 否 | 否 | 否 | 仅 `repo local`，云端每次都是新容器 |
| `--slug/--owner` | 否 | 是 | 否 | 否 | 否 | 否 | 否 | Dataset 标识 |
| `--staging` | 否 | 是 | 否 | 否 | 否 | 否 | 否 | Dataset 临时目录 |
| `--update` | 否 | 是 | 否 | 否 | 否 | 否 | 否 | 更新已存在 Dataset |
| `-m/--message` | 否 | 是 | 否 | 否 | 否 | 否 | 否 | 仅与 `dataset --update` 有意义 |
| `-y/--yes` | 否 | 是 | 是 | 否 | 否 | 否 | 否 | 跳过 dataset/push 确认 |
| `-d/--dataset` | 否 | 否 | 是 | 否 | 否 | 否 | 否 | `push`，可重复 |
| `--wait` | 否 | 否 | 否 | 是 | 否 | 否 | 否 | `run` 等待秒数 |

注意 `-m` 在不同子命令中含义不同：在 `local/run/log/download` 中是 `model`，在
`dataset` 中是版本 `message`。参数含义由紧邻的子命令决定。

以下组合会失败或不起作用：

```bash
# 失败：全局参数位置错误
python run_cloud.py local -t repo --repo ./examples/example_repo

# 失败：两个提示词来源互斥
python run_cloud.py -t repo local --repo ./examples/example_repo \
  -p "修复测试" --prompt-file ./examples/prompts/fix-textstats.md

# 失败：提示词目前只支持 repo
python run_cloud.py -t spec local --spec ./examples/spec/usage-api.md -p "生成客户端"

# 失败：dataset 的 --repo 是必填参数
python run_cloud.py -t repo dataset --slug cloud-codegen-example

# 不起作用：首次 create 不使用版本说明
python run_cloud.py -t repo dataset --repo ./examples/example_repo \
  --message "first version"
```

### 9.10 三套完整流程组合

只在本地试跑，不上传代码：

```bash
python run_cloud.py -t repo local \
  --repo ./examples/example_repo \
  --prompt-file ./examples/prompts/fix-textstats.md \
  --output ./runs/local-example
```

首次云端运行：

```bash
python run_cloud.py -t repo local \
  --repo ./examples/example_repo \
  --prompt-file ./examples/prompts/fix-textstats.md \
  --output ./runs/local-example

python run_cloud.py -t repo dataset \
  --repo ./examples/example_repo \
  --owner YOUR_KAGGLE_USERNAME \
  --slug cloud-codegen-example \
  --prompt-file ./examples/prompts/fix-textstats.md

python run_cloud.py -t repo push \
  -d YOUR_KAGGLE_USERNAME/cloud-codegen-example

python run_cloud.py -t repo run \
  --model anthropic/claude-sonnet-5@default \
  --wait 900

python run_cloud.py -t repo status

python run_cloud.py -t repo log \
  --model anthropic/claude-sonnet-5@default

python run_cloud.py -t repo download \
  --model anthropic/claude-sonnet-5@default \
  --output ./runs/cloud-example
```

修改源码或提示词后更新并重跑：

```bash
python run_cloud.py -t repo local \
  --repo ./examples/example_repo \
  --prompt-file /absolute/path/to/task-v2.md \
  --output ./runs/local-v2

python run_cloud.py -t repo dataset \
  --repo ./examples/example_repo \
  --owner YOUR_KAGGLE_USERNAME \
  --slug cloud-codegen-example \
  --prompt-file /absolute/path/to/task-v2.md \
  --update \
  --message "source and prompt v2"

python run_cloud.py -t repo push \
  -d YOUR_KAGGLE_USERNAME/cloud-codegen-example

python run_cloud.py -t repo run --wait 900

python run_cloud.py -t repo download \
  --output ./runs/cloud-v2
```

`dataset` 更新后必须重新 `push`，让新的 task 版本明确挂载目标 Dataset；然后再 `run`。
`run_cloud.py` 不会把这些阶段自动串起来，也不会自动应用下载的补丁。

### 9.11 与 Kaggle 原生命令的对应关系

| `run_cloud.py` | 底层主要命令 |
|---|---|
| `local` | 直接运行 `tasks/repo_task.py`、`tasks/spec_task.py` 或 `tasks/codegen_task.py` |
| `dataset` 首次创建 | `kaggle datasets create -p STAGING --dir-mode zip` |
| `dataset --update` | `kaggle datasets version -p STAGING -m MESSAGE --dir-mode zip` |
| `push` | `kaggle b t push SLUG -f TASK_FILE --wait 600 [-d DATASET ...]` |
| `run` | `kaggle b t run SLUG -m MODEL [--wait SECONDS]` |
| `status` | `kaggle b t status SLUG` |
| `log` | `kaggle b t log SLUG -m MODEL` |
| `download` | `kaggle b t download SLUG -m MODEL -o OUTPUT` |

原生命令的完整参数、认证方式、多模型运行和脚本未暴露的选项见
[KAGGLE_CLI.md](docs/KAGGLE_CLI.md)。
