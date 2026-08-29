"""让 tests 无论从哪个目录运行都能 import kllm。"""

import sys
from pathlib import Path

PACKAGE_PARENT = str(Path(__file__).resolve().parents[1])
if PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, PACKAGE_PARENT)
