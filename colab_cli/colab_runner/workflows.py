"""GET /workflows 与 POST /workflows/{slug}/frames（doc/02 §2.2）。

- 列表：扫描 `workflows/*.json`，文件是真相（doc/03 §3 WorkflowConfig 的
  同步来源）；单个文件损坏不拖垮整个列表。
- 设帧：包装 `set_frames.py` 子进程（不改它，doc/06 原则）。
  ⚠️ 它用 `os.replace()` **就地覆写**目标 api.json（共享可变状态）；
  已受理任务靠 §2.3.1 的提交快照隔离，不受影响。
  ⚠️ `--last` 能否用取决于目标工作流的 `MiniMaxH3ImageToVideo` 是否接了
  `last_frame`。仓库根 `api.json`（minimax_h3）已接节点 122；没接的工作流
  set_frames.py 会明确报错，这里透传为 400。
"""
import subprocess
import sys
from pathlib import Path
from typing import Optional

from colab_core.inputs import load_json, resolve_api_json
from colab_core.paths import HERE

WORKFLOWS_DIR = HERE / "workflows"
IMAGE_DIR = HERE / "image"
SET_FRAMES = HERE / "set_frames.py"


class WorkflowError(RuntimeError):
    """workflows 接口的业务错误（app 层映射 HTTP）。"""

    def __init__(self, message: str, http_status: int = 400,
                 code: str = "WORKFLOW_ERROR", detail: Optional[dict] = None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.detail = detail or {}


def _workflow_config_path(slug: str) -> Path:
    workflows_dir = Path(WORKFLOWS_DIR)
    path = (workflows_dir / f"{slug}.json").resolve()
    # 防路径穿越：slug 必须落在 workflows/ 目录内
    if workflows_dir.resolve() not in path.parents or not path.is_file():
        raise WorkflowError(f"未知工作流: {slug}", http_status=404,
                            code="WORKFLOW_NOT_FOUND")
    return path


def list_workflows() -> list[dict]:
    """扫描 workflows/*.json，返回 slug/name/模型清单/总体积/支持的加速器。"""
    workflows_dir = Path(WORKFLOWS_DIR)
    entries = []
    for path in sorted(workflows_dir.glob("*.json")):
        try:
            config = load_json(path, "工作流配置")
            models = config.get("models") or []
            total_bytes = sum(int(model.get("size_bytes", 0)) for model in models)
            entries.append({
                "slug": path.stem,
                "name": config.get("name", path.stem),
                "config_path": str(path),
                "api_json": str(resolve_api_json(None, path, config)),
                "supported_accelerators": config.get(
                    "supported_accelerators", ["gpu"]),
                "accelerator_note": config.get("accelerator_note"),
                "input_fields": config.get("input_fields", []),
                "editable_fields": config.get("editable_fields", []),
                "models": models,
                "total_model_bytes": total_bytes,
            })
        except Exception as error:  # 单个配置损坏不拖垮列表
            entries.append({"slug": path.stem, "error": str(error)})
    return entries


def set_frames(slug: str, first: Optional[str], last: Optional[str]) -> dict:
    """包装 set_frames.py：复制图片到 image/ 并就地更新目标 api.json。"""
    if not first and not last:
        raise WorkflowError("至少需要提供 first 或 last 之一", code="BAD_REQUEST")
    config_path = _workflow_config_path(slug)
    config = load_json(config_path, "工作流配置")
    api_json = resolve_api_json(None, config_path, config)

    command = [
        sys.executable, str(SET_FRAMES),
        "--api-json", str(api_json),
        "--image-dir", str(IMAGE_DIR),
    ]
    for flag, value in (("--first", first), ("--last", last)):
        if value:
            source = Path(value).expanduser()
            if not source.is_file():
                raise WorkflowError(f"图片不存在: {value}", code="INPUT_NOT_FOUND")
            command += [flag, str(source)]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkflowError(f"set_frames.py 执行失败: {error}",
                            http_status=500, code="FRAMES_UPDATE_FAILED") from error
    if result.returncode != 0:
        # set_frames.py 的用户级错误（sys.exit("错误: ...")）在 stderr
        message = (result.stderr or result.stdout or "").strip().splitlines()
        raise WorkflowError(
            message[-1] if message else "set_frames.py 失败",
            code="FRAMES_UPDATE_FAILED",
            detail={"slug": slug, "api_json": str(api_json)},
        )
    return {
        "ok": True,
        "slug": slug,
        "api_json": str(api_json),
        "image_dir": str(IMAGE_DIR),
        "output": (result.stdout or "").strip(),
    }
