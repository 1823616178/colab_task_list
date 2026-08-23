"""路径常量与输出目录解析。

HERE 锁定在 colab_cli 包根（与 run.py 的 Path(__file__).parent 一致），
start.sh / run.py / workflows/ 的相对位置约束见 doc/06 §C2。
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REMOTE_SETUP = HERE / "remote_setup.py"
REMOTE_WORKFLOW = HERE / "remote_workflow.py"
DEFAULT_WORKFLOW_CONFIG = HERE / "workflows" / "minimax_h3.json"


def default_output_root() -> Path:
    """输出根目录：COLAB_OUTPUT_DIR 环境变量优先，默认 HERE/colab_output。"""
    override = os.environ.get("COLAB_OUTPUT_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return HERE / "colab_output"
