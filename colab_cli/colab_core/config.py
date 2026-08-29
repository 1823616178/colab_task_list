"""run_task 的输入配置与结果。

字段与 doc/02-接口契约.md §2.3 `POST /tasks` 请求体一一对应，
同时覆盖 run.py argparse 的全部参数。
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

GPU_SPECS = ("T4", "L4", "G4", "H100", "A100")
TPU_SPECS = ("v5e1", "v6e1")
ACCELERATOR_KINDS = ("gpu", "tpu")


@dataclass
class TaskConfig:
    """一次任务的完整输入。

    - `transport_mode`：`None` 时读 COLAB_TRANSPORT 环境变量，默认 native
      （探测失败自动降级 cli）。
    - `recovery_command`：失败保护提示中的重试命令。CLI 传入
      `python run.py --session ... --download-only`；服务端传 None，
      提示走 `POST /tasks/{id}/redownload`。
    """

    workflow_config: Path
    session: str = "comfyui-h3"
    api_json: Optional[Path] = None
    accelerator_kind: str = "gpu"
    accelerator_spec: str = "T4"
    mount_drive: bool = True
    reuse: bool = False
    keep: bool = False
    input_dir: Optional[Path] = None
    workflow_overrides: dict[str, str] = field(default_factory=dict)
    timeout: float = 86400.0
    dry_run: bool = False
    task_id: Optional[str] = None
    output_dir: Optional[Path] = None
    transport_mode: Optional[str] = None
    recovery_command: Optional[str] = None

    def __post_init__(self) -> None:
        self.workflow_config = Path(self.workflow_config).expanduser().resolve()
        if self.api_json is not None:
            self.api_json = Path(self.api_json).expanduser().resolve()
        if self.input_dir is not None:
            self.input_dir = Path(self.input_dir).expanduser().resolve()
        if self.output_dir is not None:
            self.output_dir = Path(self.output_dir).expanduser().resolve()
        if not isinstance(self.workflow_overrides, dict):
            raise TypeError("workflow_overrides 必须是对象")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.workflow_overrides.items()
        ):
            raise TypeError("workflow_overrides 的键和值必须是字符串")
        if self.accelerator_kind not in ACCELERATOR_KINDS:
            raise ValueError(f"未知加速器类型: {self.accelerator_kind!r}")
        valid_specs = GPU_SPECS if self.accelerator_kind == "gpu" else TPU_SPECS
        if self.accelerator_spec not in valid_specs:
            raise ValueError(
                f"{self.accelerator_kind} 不支持规格 {self.accelerator_spec!r}，"
                f"可选: {', '.join(valid_specs)}"
            )

    @classmethod
    def from_dict(cls, payload: dict) -> "TaskConfig":
        """从 doc/02 §2.3 的 POST /tasks 请求体构造。"""
        accelerator = payload.get("accelerator") or {}
        return cls(
            workflow_config=Path(payload["workflow_config"]),
            session=payload.get("session", "comfyui-h3"),
            api_json=(
                Path(payload["api_json"]) if payload.get("api_json") else None
            ),
            accelerator_kind=accelerator.get("kind", "gpu"),
            accelerator_spec=accelerator.get("spec", "T4"),
            mount_drive=bool(payload.get("mount_drive", True)),
            reuse=bool(payload.get("reuse", False)),
            keep=bool(payload.get("keep", False)),
            input_dir=(
                Path(payload["input_dir"]) if payload.get("input_dir") else None
            ),
            workflow_overrides=payload.get("workflow_overrides") or {},
            timeout=float(payload.get("timeout", 86400.0)),
            dry_run=bool(payload.get("dry_run", False)),
        )


@dataclass
class TaskResult:
    """任务成功终态的结果摘要（task.done 事件的 data 来源）。"""

    task_id: str
    status: str
    output_zip: Path
    output_size_bytes: int
    output_sha256: str
    session_kept: bool
    transport_mode: str
    duration_ms: int
    extra: dict = field(default_factory=dict)
