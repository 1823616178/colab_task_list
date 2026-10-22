"""生成可单文件 push 的 Benchmark 任务(等价于 kcloud build)。

用法: python scripts/build_tasks.py [输出目录, 默认 build/tasks]
"""

import sys
from pathlib import Path

from kaggle_tools.cloud.build import build_tasks

if __name__ == "__main__":
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "build" / "tasks"
    for path in build_tasks(output):
        print(path)
