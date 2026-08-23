# kaggle-benchmarks 任务语法参考

- 安装: `pip install kaggle-benchmarks`
- [快速上手](https://github.com/Kaggle/kaggle-benchmarks/blob/ci/quick_start.md)
- [示例手册(Cookbook)](https://github.com/Kaggle/kaggle-benchmarks/blob/ci/cookbook.md)

## 装饰器与函数签名

```python
@kbench.task(name="task_name", description="...", version=1)
def my_task(llm, param1: str, param2: int) -> ReturnType:
    ...
```

- 第一个参数固定是 `llm`(被测模型)。
- 额外参数通过 `.run()` 或 `.evaluate()` 传入。
- `name` 默认取函数名; `description` 默认取 docstring。
- **重要:** `name` 会被规范化为 URL 安全的 slug(例如 `"My Task"` 变成 `"my-task"`)。
  该 slug 必须与 `kaggle b t push <task-slug> -f <file>` 使用的任务名一致。

## 返回类型注解(控制排行榜的展示方式)

| 注解 | 含义 |
|---|---|
| `None` / 不写 | 通过/失败 —— 完全由断言判定 |
| `-> bool` | 二值通过/失败 |
| `-> int` / `-> float` | 数值分数 |
| `-> tuple[int, int]` | (通过数, 总数)计数 |
| `-> tuple[float, float]` | (数值, 置信区间) |
| `-> dict` | 结构化结果字典 |

## LLM 交互

```python
response = llm.prompt("question")                            # 返回 str
obj = llm.prompt("question", schema=MyDataclass)             # 结构化输出
response = llm.prompt("q", image=images.from_url(url))       # 带图片
response = llm.prompt("q", video=videos.from_url(yt_url))    # 带视频
response = llm.prompt("q", audio=audios.from_path(path))     # 带音频
response = llm.prompt("q", tools=[my_func])                  # 工具调用(需要 api="genai")
llm.send(msg)        # 追加消息但不触发回复
llm.respond()         # 基于已有对话继续获取回复
kbench.user.send(x)   # 向对话历史追加一条 user 消息(文本/图片等)
```

## 对话上下文管理

```python
with kbench.chats.new("name"):     # 独立的对话上下文
    response = llm.prompt("...")    # 只发送这个对话的历史
```

循环中建议用 `kbench.chats.new()`, 避免上下文无限增长。
多智能体场景: `contexts.enter(chat=agent_chat)`

## 访问模型

```python
kbench.llm                              # 默认模型占位符
kbench.judge_llm                        # 裁判模型(用于评判输出)
kbench.llms["google/gemini-2.5-flash"]  # 按名称指定模型
```

## 断言(务必带上 `expectation=`, 用于排行榜展示)

```python
kbench.assertions.assert_equal(expected, actual, expectation="...")
kbench.assertions.assert_true(value, expectation="...")
kbench.assertions.assert_false(value, expectation="...")
kbench.assertions.assert_in(member, container, expectation="...")
kbench.assertions.assert_not_in(member, container, expectation="...")
kbench.assertions.assert_contains_regex(pattern, text, expectation="...")
kbench.assertions.assert_not_contains_regex(pattern, text, expectation="...", flags=0)
kbench.assertions.assert_empty(container, expectation="...")
kbench.assertions.assert_not_empty(container, expectation="...")
kbench.assertions.assert_fail(expectation="...")  # 无条件失败
```

## 自定义断言

```python
from kaggle_benchmarks.assertions import assertion_handler, AssertionResult

@assertion_handler()
def assert_is_positive(value: float, expectation: str) -> AssertionResult:
    return AssertionResult(passed=value > 0, expectation=expectation)
```

## 基于裁判模型的评估

```python
report = kbench.assertions.assess_response_with_judge(
    criteria=("标准 1", "标准 2"),
    response_text=response,
    judge_llm=kbench.judge_llm,
    prompt_fn=optional_custom_fn,       # (criteria, response_text) -> str
    output_schema=OptionalDataclass,
)
for r in report.results:
    kbench.assertions.assert_true(r.passed, expectation=f"{r.criterion}: {r.reason}")
```

## 运行任务

必须调用 `.run()` 或 `.evaluate()` 才会生成运行文件。
不调用的话不会产生 `.run.json`, 什么都不会被记录。

```python
# 单次运行:
run = my_task.run(llm=kbench.llm, param1="val1", param2=42)

# 数据集评估(对 DataFrame 的每一行各跑一次任务):
runs = my_task.evaluate(
    llm=[kbench.llm], evaluation_data=df,  # df 的列名对应任务参数
    n_jobs=2, timeout=120,
    stop_condition=lambda r: len(r) == df.shape[0],
    max_attempts=50, retry_delay=15, remove_run_files=True,
)
results_df = runs.as_dataframe()
```

## 多模态内容工厂

```python
from kaggle_benchmarks.content_types import images, videos, audios
images.from_url(url) | images.from_path(p) | images.from_base64(b64, format="png")
videos.from_url(youtube_url)
audios.from_path(p) | audios.from_url(url) | audios.from_base64(b64, format="mp3")
```

## Token 用量统计

```python
with kbench.chats.new("chat") as chat:
    llm.prompt("...")
    chat.usage.input_tokens / .output_tokens / .input_tokens_cost_nanodollars
```
