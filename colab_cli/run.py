# =============================================================================
# 本地调度器（CLI 薄壳）：解析参数、打印中文事件。
# 编排逻辑在 colab_core.run_task()；Colab 通道在 colab_transport/。
# 行为约束：start.sh / stop.sh 的交互与输出与旧版逐字一致（doc/05 P0 验收）。
# =============================================================================
import argparse
import shutil
import sys
from pathlib import Path

from colab_core import (
    DEFAULT_WORKFLOW_CONFIG,
    EventEmitter,
    TaskConfig,
    TaskError,
    create_task_id,
    default_output_root,
    make_cli_emit,
    run_task,
)
from colab_core.config import ALL_GPU_SPECS, BACKENDS, MODEL_MODES, resolve_backend
from colab_core.paths import HERE, REMOTE_SETUP, REMOTE_WORKFLOW
from colab_core.sessions import (
    ensure_reusable_session,
    stop_named_session,
    stop_unmapped_endpoint,
)
from colab_transport import TransportError, get_transport

# ── 兼容旧测试与外部引用（tests/test_output_transfer.py 用 runner.X）──
from colab_transport.constants import (
    COLAB_EXEC_MAX_ATTEMPTS,
    COLAB_OUTPUT_TAIL_CHARS,
    DOWNLOAD_MAX_ATTEMPTS,
    DOWNLOAD_RETRY_DELAY_SECONDS,
    OUTPUT_STREAM_PREFIX,
    REMOTE_OUTPUT_ARCHIVE,
    REMOTE_TRANSFER_ACK,
    REMOTE_TRANSFER_ACTIVE,
    REMOTE_TRANSFER_DIR,
    REMOTE_TRANSFER_STREAM_REQUEST,
)
from colab_transport.native import OutputStreamReceiver

__all__ = [
    "COLAB_EXEC_MAX_ATTEMPTS",
    "COLAB_OUTPUT_TAIL_CHARS",
    "DOWNLOAD_MAX_ATTEMPTS",
    "DOWNLOAD_RETRY_DELAY_SECONDS",
    "HERE",
    "OUTPUT_STREAM_PREFIX",
    "REMOTE_OUTPUT_ARCHIVE",
    "REMOTE_SETUP",
    "REMOTE_TRANSFER_ACK",
    "REMOTE_TRANSFER_ACTIVE",
    "REMOTE_TRANSFER_DIR",
    "REMOTE_TRANSFER_STREAM_REQUEST",
    "REMOTE_WORKFLOW",
    "OutputStreamReceiver",
    "create_task_id",
    "ensure_reusable_session",
    "stop_named_session",
    "stop_unmapped_endpoint",
]


def main():
    """解析配置并调度一次完整的 Colab 工作流任务。"""
    parser = argparse.ArgumentParser()
    accelerator_group = parser.add_mutually_exclusive_group()
    accelerator_group.add_argument(
        "--gpu",
        choices=ALL_GPU_SPECS,
        default=None,
        help="GPU 规格（按后端校验）；未指定 GPU/TPU 时默认 T4",
    )
    accelerator_group.add_argument(
        "--tpu",
        choices=("v5e1", "v6e1"),
        default=None,
        help="TPU 规格；仅适用于声明支持 TPU 的工作流",
    )
    parser.add_argument(
        "--backend",
        choices=BACKENDS,
        default=None,
        help="计算后端：colab（默认）| kaggle | modal；也可用 COMPUTE_BACKEND 环境变量",
    )
    parser.add_argument("--session", default="comfyui-h3")
    parser.add_argument("--workflow-config", default=str(DEFAULT_WORKFLOW_CONFIG),
                        help="工作流配置 JSON，默认 workflows/minimax_h3.json")
    parser.add_argument("--api-json", default=None,
                        help="覆盖工作流配置中的 api_json")
    parser.add_argument(
        "--input-dir",
        default=None,
        help="覆盖所有输入素材的搜索目录；默认按工作流 local_dir 分别查找",
    )
    parser.add_argument("--timeout", type=float, default=86400.0,
                        help="每个远端阶段的超时秒数，0 表示无限制")
    parser.add_argument(
        "--mount-drive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否挂载 Google Drive 并复用模型缓存",
    )
    parser.add_argument(
        "--model-mode",
        choices=MODEL_MODES,
        default="auto",
        help=(
            "模型获取方式：auto=后端默认；download=直接从 Hugging Face 下载到 VM；"
            "symlink=复用 Drive 缓存并软链接；copy=复用 Drive 缓存并复制到 VM"
        ),
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="复用已存在的会话，跳过创建 VM 和通用环境安装",
    )
    parser.add_argument("--keep", action="store_true", help="跑完保留 VM")
    parser.add_argument(
        "--use-last-frame",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="启用可选尾帧；仅适用于声明 optional_last_frame 的工作流，默认关闭",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练完整事件流程但不建 VM、不下模型、不调 colab（开发联调用）",
    )
    operation_group = parser.add_mutually_exclusive_group()
    operation_group.add_argument("--stop", action="store_true", help="只销毁命名会话")
    operation_group.add_argument(
        "--stop-endpoint",
        default=None,
        help="按 Runtime endpoint 销毁缺少本地名称映射的会话",
    )
    operation_group.add_argument(
        "--download-only",
        action="store_true",
        help="只从保留的会话取回已生成输出，不重新执行工作流",
    )
    args = parser.parse_args()

    backend = resolve_backend(args.backend)
    args.backend = backend
    if backend == "colab" and not args.dry_run and not shutil.which("colab"):
        sys.exit("找不到 colab 命令, 请先: pip install google-colab-cli")

    if args.stop:
        if backend == "colab":
            stop_named_session(args.session)
        else:
            get_transport(None, backend=backend).stop_session(args.session)
            print(f">>> 已停止 {backend} 会话: {args.session}", flush=True)
        return
    if args.stop_endpoint:
        stop_unmapped_endpoint(args.stop_endpoint)
        return

    emit = make_cli_emit()

    if args.download_only:
        recovery_id = f"recovered-{create_task_id()}"
        emitter = EventEmitter(recovery_id, emit)

        def sink(phase, status, message, data=None):
            emitter.emit(phase, status, message, data)

        transport = get_transport(None, sink=sink, backend=args.backend)
        ensure_reusable_session(args.session, transport, emitter)
        recovery_dir = default_output_root() / recovery_id
        recovery_dir.mkdir(parents=True, exist_ok=True)
        output_zip = recovery_dir / "output.zip"
        transport.download_output(args.session, output_zip, sink)
        print("结果已恢复到:", output_zip)
        print(f">>> 会话继续保留: {args.session}", flush=True)
        return

    accelerator_kind = "tpu" if args.tpu else "gpu"
    accelerator_spec = args.tpu or args.gpu or "T4"
    config = TaskConfig(
        workflow_config=Path(args.workflow_config),
        session=args.session,
        api_json=Path(args.api_json) if args.api_json else None,
        accelerator_kind=accelerator_kind,
        accelerator_spec=accelerator_spec,
        mount_drive=args.mount_drive,
        model_mode=args.model_mode,
        reuse=args.reuse,
        keep=args.keep,
        input_dir=Path(args.input_dir) if args.input_dir else None,
        use_last_frame=args.use_last_frame,
        timeout=args.timeout,
        dry_run=args.dry_run,
        backend=args.backend,
        recovery_command=(
            f"{sys.executable} {Path(__file__).resolve()} "
            f"--session {args.session} --download-only"
        ),
    )
    try:
        run_task(config, emit)
    except (TaskError, TransportError):
        # 错误文案已随 error 事件打印（与旧 sys.exit 输出一致）
        sys.exit(1)


if __name__ == "__main__":
    main()
