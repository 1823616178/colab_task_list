"""测试公共夹具: 加载构建后的单文件任务, 以及 live 测试开关。"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from kaggle_tools.cloud.build import build_tasks

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"


@pytest.fixture(scope="session")
def built_tasks_dir(tmp_path_factory) -> Path:
    """整个会话只构建一次单文件任务。"""
    output = tmp_path_factory.mktemp("built_tasks")
    build_tasks(output)
    return output


@pytest.fixture
def load_task(built_tasks_dir, tmp_path, monkeypatch):
    """按文件名导入一个构建后的任务模块, 工作区指向 tmp_path。"""

    def _load(file_name: str):
        monkeypatch.setenv("CODEGEN_IMPORT_ONLY", "1")
        monkeypatch.chdir(tmp_path)
        module_name = f"built_{Path(file_name).stem}"
        spec = importlib.util.spec_from_file_location(module_name, built_tasks_dir / file_name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "OUTPUT_DIRECTORY", tmp_path)
        monkeypatch.setattr(module, "WORKSPACE", tmp_path / "workspace")
        return module

    yield _load
    for name in [n for n in sys.modules if n.startswith("built_")]:
        sys.modules.pop(name, None)


def pytest_collection_modifyitems(config, items):
    """live 测试除了 -m live, 还要求 KAGGLE_TOOLS_LIVE=1, 防止误烧额度。"""
    if os.environ.get("KAGGLE_TOOLS_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="设置 KAGGLE_TOOLS_LIVE=1 才运行 live 测试")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
