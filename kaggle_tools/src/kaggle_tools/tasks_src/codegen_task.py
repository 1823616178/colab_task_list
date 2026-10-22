"""云端代码生成评测任务: 模型写代码 -> 真跑测试 -> 断言取自测试结果。

这是一个真评测: 断言用的是生成代码在测试下的实际退出码, 不是占位断言。
生成的源码和测试报告会写进工作目录, 随 run 输出打包, 可用 kaggle b t download 取回。

改任务只需要改下面三个常量(SPEC / TESTS / MODULE_NAME), 然后重新 push。
"""

import re
import subprocess
import sys
from pathlib import Path

import kaggle_benchmarks as kbench

from _workspace_tools import *  # kcloud build 时内联

# ---- 要模型写什么 ----------------------------------------------------------

MODULE_NAME = "interval_merge"

SPEC = """\
写一个 Python 模块, 只用标准库, 提供一个函数:

    def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]

把一组可能重叠的闭区间合并成不重叠的区间列表, 按起点升序返回。
要求:
- 相邻但不重叠的区间不合并, 例如 (1, 2) 和 (3, 4) 保持分开
- 端点相接的区间要合并, 例如 (1, 3) 和 (3, 5) 合并为 (1, 5)
- 输入为空时返回空列表
- 起点大于终点的区间视为非法, 抛 ValueError
- 不修改传入的列表
- 带类型注解和 docstring
"""

# 评测的事实依据: 这些测试就是及格线, 模型看不到它们。
TESTS = """\
import pytest

from interval_merge import merge_intervals


def test_merges_overlapping():
    assert merge_intervals([(1, 4), (2, 6), (8, 10)]) == [(1, 6), (8, 10)]


def test_keeps_adjacent_but_disjoint_apart():
    assert merge_intervals([(1, 2), (3, 4)]) == [(1, 2), (3, 4)]


def test_merges_touching_endpoints():
    assert merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]


def test_sorts_output_by_start():
    assert merge_intervals([(8, 10), (1, 4)]) == [(1, 4), (8, 10)]


def test_empty_input():
    assert merge_intervals([]) == []


def test_rejects_reversed_interval():
    with pytest.raises(ValueError):
        merge_intervals([(5, 1)])


def test_does_not_mutate_input():
    original = [(3, 5), (1, 2)]
    snapshot = list(original)
    merge_intervals(original)
    assert original == snapshot
"""

# 测试失败后允许模型修复几轮; 0 表示一次成型。
MAX_REPAIR_ROUNDS = 2

CODE_BLOCK_PATTERN = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def extract_code(response: str) -> str:
    """从模型回复里取出代码; 没有 markdown 围栏就当作整段都是代码。"""
    blocks = CODE_BLOCK_PATTERN.findall(response)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return response.strip() + "\n"


def run_tests(work_directory: Path) -> subprocess.CompletedProcess:
    """在工作目录里跑 pytest, 返回结果供断言使用。"""
    return run_command(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"test_{MODULE_NAME}.py"],
        cwd=work_directory,
    )


@kbench.task(
    name="cloud-codegen",
    description="让模型实现区间合并函数, 用一组它看不到的测试判定是否通过",
)
def cloud_codegen(llm) -> bool:
    """模型写代码, 跑测试, 失败则带着报错让它修, 断言取最终测试结果。"""
    work_directory = OUTPUT_DIRECTORY
    work_directory.mkdir(parents=True, exist_ok=True)
    (work_directory / f"test_{MODULE_NAME}.py").write_text(TESTS, encoding="utf-8")

    module_path = work_directory / f"{MODULE_NAME}.py"
    response = llm.prompt(
        f"{SPEC}\n模块文件名是 {MODULE_NAME}.py。只输出代码, 不要解释。"
    )
    module_path.write_text(extract_code(response), encoding="utf-8")

    result = run_tests(work_directory)
    for attempt in range(MAX_REPAIR_ROUNDS):
        if result.returncode == 0:
            break
        print(f"=== 第 {attempt + 1} 轮测试未通过, 让模型修复 ===", flush=True)
        print(result.stdout[-2000:], flush=True)
        response = llm.prompt(
            "你写的代码没通过测试, 下面是 pytest 输出:\n\n"
            f"{result.stdout[-4000:]}\n\n"
            "修好它。只输出完整的模块代码, 不要解释。"
        )
        module_path.write_text(extract_code(response), encoding="utf-8")
        result = run_tests(work_directory)

    report_path = work_directory / f"{MODULE_NAME}_pytest.txt"
    report_path.write_text(
        f"exit_code={result.returncode}\n\n{result.stdout}\n{result.stderr}", encoding="utf-8"
    )

    print("=== 最终代码 ===", flush=True)
    print(module_path.read_text(encoding="utf-8"), flush=True)
    print("=== pytest ===", flush=True)
    print(result.stdout[-3000:], flush=True)

    kbench.assertions.assert_equal(
        0, result.returncode,
        expectation=f"生成的 {MODULE_NAME}.py 要通过全部 {TESTS.count('def test_')} 个测试",
    )
    return result.returncode == 0


if not import_only():
    cloud_codegen.run(kbench.llm)  # type: ignore[attr-defined]
