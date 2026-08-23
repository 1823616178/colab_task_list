# 用 Kaggle AI 额度调用 LLM 生成代码的最小示例
# 任务名会被规范化为 URL slug: codegen-task
import kaggle_benchmarks as kbench


@kbench.task(
    name="codegen-task",
    description="让 LLM 写一个快排函数, 检查输出是否包含函数定义",
)
def codegen(llm) -> None:
    response = llm.prompt(
        "Write a Python function `quick_sort(arr)` implementing quicksort, "
        "with a docstring and type hints. Output ONLY the code."
    )
    print("=== 模型输出 ===")
    print(response)
    kbench.assertions.assert_in(
        "def quick_sort", response,
        expectation="输出中应包含 quick_sort 函数定义",
    )


# 必须调用 run() 才会生成运行记录
# 注: 装饰器运行时会把函数包装成 Task 对象, 但类型标注未体现,
# Pylance 会误报 "Cannot access attribute run", 故加 type: ignore 屏蔽
codegen.run(kbench.llm)  # type: ignore[attr-defined]
