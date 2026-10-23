"""run_task：一次 Colab 工作流任务的完整编排。

自 run.py main() 迁入；不认识 argparse、不认识 print —— 全部输出经
EventEmitter 转成 TaskEvent（doc/02 §1），CLI 薄壳用 make_cli_emit()
逐字还原旧输出。

关键规则（doc/02 §2.3.1）：受理时立即快照 api.json / 工作流配置 /
输入素材到任务目录，之后一切引用快照 —— 历史任务从此可完整复现。
"""
import json
import math
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from colab_transport import TransportError, WorkflowNotSubmittedError, get_transport
from colab_transport.transfer import file_sha256

from . import events as ev
from .config import TaskConfig, TaskResult
from .errors import TaskError
from .events import EventEmitter
from .inputs import (
    apply_optional_last_frame,
    build_search_dirs,
    collect_input_references,
    load_json,
    resolve_api_json,
    resolve_input_files,
    validate_workflow_accelerator,
)
from .paths import HERE, REMOTE_SETUP, REMOTE_WORKFLOW, default_output_root
from .resilience import is_transient_error, reattach_workflow, retry_transient
from .sessions import ensure_reusable_session


def _coerce_workflow_override(
    key: str,
    value: str,
    definition: dict,
) -> str | int | float:
    """Validate and convert a string override to the API node input type."""
    options = definition.get("options")
    if options is not None:
        if not isinstance(options, list) or not all(
            isinstance(option, dict)
            and isinstance(option.get("value"), str)
            for option in options
        ):
            raise TaskError(f"可编辑参数 {key} 的 options 配置无效")
        if value not in {option["value"] for option in options}:
            raise TaskError(f"可编辑参数 {key} 不在允许的选项中")

    value_type = definition.get("value_type", "string")
    if value_type == "string":
        return value
    if value_type == "number":
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise TaskError(f"可编辑参数 {key} 必须是有效数字") from error
        if not math.isfinite(number):
            raise TaskError(f"可编辑参数 {key} 必须是有效数字")
        return int(number) if number.is_integer() else number
    raise TaskError(f"可编辑参数 {key} 的 value_type 不受支持: {value_type}")


def create_task_id() -> str:
    """生成适合目录名的唯一任务编号（格式不变：UTC 时间戳 + uuid4 前 8 位）。"""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _snapshot_inputs(out_dir: Path, staged_inputs: list) -> None:
    """把输入素材硬链接（同盘省空间；跨盘回退复制）到任务目录。"""
    inputs_dir = out_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    for item in staged_inputs:
        destination = inputs_dir / Path(item["workflow_path"]).name
        destination.unlink(missing_ok=True)
        try:
            os.link(item["local_path"], destination)
        except OSError:
            shutil.copyfile(item["local_path"], destination)
        item["snapshot_path"] = destination


def _apply_workflow_overrides(
    workflow: dict,
    editable_fields: list,
    overrides: dict[str, str],
) -> None:
    """按工作流声明把任务级覆盖写入内存中的 API 工作流。

    覆盖只能命中 ``editable_fields`` 白名单。未指定 ``node_id`` 时，
    ``class_type`` 必须在 API 工作流中唯一，避免悄悄改错同类节点。
    """
    if not overrides:
        return
    if not isinstance(editable_fields, list):
        raise TaskError("工作流配置 editable_fields 必须是列表")

    definitions = {}
    for definition in editable_fields:
        if not isinstance(definition, dict):
            raise TaskError("工作流配置 editable_fields 中的每项必须是对象")
        key = definition.get("key")
        if not isinstance(key, str) or not key:
            raise TaskError("工作流配置 editable_fields 缺少有效 key")
        if key in definitions:
            raise TaskError(f"工作流配置 editable_fields 存在重复 key: {key}")
        definitions[key] = definition

    unknown = sorted(set(overrides) - set(definitions))
    if unknown:
        raise TaskError(f"工作流不支持参数覆盖: {', '.join(unknown)}")

    for key, value in overrides.items():
        definition = definitions[key]
        field_name = definition.get("field")
        node_id = definition.get("node_id")
        class_type = definition.get("class_type")
        if not isinstance(field_name, str) or not field_name:
            raise TaskError(f"可编辑参数 {key} 缺少有效 field")
        if node_id is not None and (not isinstance(node_id, str) or not node_id):
            raise TaskError(f"可编辑参数 {key} 的 node_id 必须是非空字符串")
        if node_id is None and (
            not isinstance(class_type, str) or not class_type
        ):
            raise TaskError(f"可编辑参数 {key} 缺少 class_type 或 node_id")

        if node_id is not None:
            candidates = [(node_id, workflow.get(node_id))]
        else:
            candidates = [
                (candidate_id, node)
                for candidate_id, node in workflow.items()
                if isinstance(node, dict)
                and node.get("class_type") == class_type
            ]
        candidates = [
            (candidate_id, node)
            for candidate_id, node in candidates
            if isinstance(node, dict)
        ]
        if not candidates:
            selector = f"节点 {node_id}" if node_id else f"{class_type} 节点"
            raise TaskError(f"可编辑参数 {key} 找不到目标 {selector}")
        if len(candidates) > 1:
            raise TaskError(
                f"可编辑参数 {key} 匹配到多个 {class_type} 节点，"
                "请在 editable_fields 中指定 node_id"
            )

        candidate_id, node = candidates[0]
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or field_name not in inputs:
            raise TaskError(
                f"可编辑参数 {key} 的目标节点 {candidate_id} 不包含输入 {field_name}"
            )
        inputs[field_name] = _coerce_workflow_override(
            key, value, definition
        )


def _snapshot_api_json(
    source: Path,
    destination: Path,
    workflow: dict,
    has_changes: bool,
) -> None:
    """写任务 API 快照；图有修改时序列化内存副本，绝不修改共享源文件。"""
    if not has_changes:
        shutil.copyfile(source, destination)
        return
    destination.write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_task(cfg: TaskConfig, emit) -> TaskResult:
    """执行一次完整任务；通过注入的 emit 产出 TaskEvent。

    成功返回 TaskResult；失败抛出 TaskError / TransportError（用户可读）
    或原始异常，并在抛出前发出 task.failed 终态事件。
    """
    started_monotonic = time.monotonic()
    task_id = cfg.task_id or create_task_id()
    emitter = EventEmitter(task_id, emit)

    if cfg.dry_run:
        try:
            return _run_dry(cfg, emitter, task_id, started_monotonic)
        except (TaskError, TransportError) as error:
            _emit_failure(emitter, cfg, error, clean=True, session_kept=False)
            raise
        except Exception as error:
            _emit_failure(emitter, cfg, error, clean=False, session_kept=False)
            raise
    return _run_real(cfg, emitter, task_id, started_monotonic)


def _run_real(
    cfg: TaskConfig,
    emitter: EventEmitter,
    task_id: str,
    started_monotonic: float,
) -> TaskResult:
    # ── 预检段（旧 main() try 块之前的部分）：失败不触碰会话 ──
    try:
        transport = get_transport(cfg.transport_mode, sink=_sink(emitter), backend=cfg.backend)
        workflow_config_path = cfg.workflow_config
        workflow_config = load_json(workflow_config_path, "工作流配置")
        api_json = resolve_api_json(cfg.api_json, workflow_config_path, workflow_config)
        workflow = load_json(api_json, "API 工作流")
        _apply_workflow_overrides(
            workflow,
            workflow_config.get("editable_fields", []),
            cfg.workflow_overrides,
        )
        frame_inputs_changed = apply_optional_last_frame(workflow, workflow_config, cfg.use_last_frame)
        input_dir_overridden = cfg.input_dir is not None
        input_dir = cfg.input_dir or (HERE / "image")
        input_references = collect_input_references(
            workflow, workflow_config.get("input_fields", []))
        search_dirs = build_search_dirs(input_dir, api_json)
        resolved_inputs = resolve_input_files(
            input_references,
            search_dirs,
            workflow_config_path,
            input_dir_overridden,
        )

        staged_inputs = []
        for index, (reference, local_path) in enumerate(resolved_inputs.items()):
            suffix = Path(reference).suffix
            remote_path = f"/content/workflow_input_{index:03d}{suffix}"
            staged_inputs.append({
                "workflow_path": reference,
                "local_path": local_path,
                "remote_path": remote_path,
            })

        workflow_name = workflow_config.get("name", workflow_config_path.stem)
        emitter.emit(
            ev.PHASE_TASK_START,
            ev.STATUS_START,
            f"工作流: {workflow_name}",
            {
                "workflow_name": workflow_name,
                "session": cfg.session,
                "accelerator": cfg.accelerator_kind,
                "variant": cfg.accelerator_spec,
                "reuse": cfg.reuse,
                "keep": cfg.keep,
                "mount_drive": cfg.mount_drive,
                "model_mode": cfg.model_mode,
                "timeout": cfg.timeout,
                "transport": transport.mode,
                "backend": cfg.backend,
            },
        )
        emitter.emit(
            ev.PHASE_INPUT_RESOLVE, ev.STATUS_START, f"API JSON: {api_json}"
        )
        emitter.emit(
            ev.PHASE_INPUT_RESOLVE,
            ev.STATUS_OK,
            f"输入文件: { {key: str(value) for key, value in resolved_inputs.items()} }",
            {
                "files": [
                    {
                        "workflow_path": item["workflow_path"],
                        "local_path": str(item["local_path"]),
                        "remote_path": item["remote_path"],
                    }
                    for item in staged_inputs
                ]
            },
        )
        emitter.emit(
            ev.PHASE_TASK_START, ev.STATUS_OK, f"任务编号: {task_id}",
            {"task_id": task_id},
        )

        # 受理即快照（doc/02 §2.3.1）：之后一切引用快照而非原文件
        out_dir = cfg.output_dir or (default_output_root() / task_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        api_snapshot = out_dir / "api.json"
        _snapshot_api_json(
            api_json, api_snapshot, workflow, bool(cfg.workflow_overrides) or frame_inputs_changed
        )
        config_snapshot = out_dir / "workflow.json"
        shutil.copyfile(workflow_config_path, config_snapshot)
        _snapshot_inputs(out_dir, staged_inputs)

        if cfg.reuse:
            emitter.emit(
                ev.PHASE_SESSION_REUSE,
                ev.STATUS_START,
                f">>> 检查并复用现有会话: {cfg.session}",
                {"session": cfg.session},
            )
            accelerator_type = ensure_reusable_session(
                cfg.session, transport, emitter
            )
            validate_workflow_accelerator(workflow_config, accelerator_type)
        else:
            accelerator_type = (
                "tpu" if cfg.accelerator_kind == "tpu" else "gpu"
            )
            validate_workflow_accelerator(workflow_config, accelerator_type)
            emitter.emit(
                ev.PHASE_SESSION_CREATE,
                ev.STATUS_START,
                "",
                {
                    "session": cfg.session,
                    "accelerator": accelerator_type,
                    "variant": cfg.accelerator_spec,
                },
            )
            transport.create_session(
                cfg.session, cfg.accelerator_kind, cfg.accelerator_spec
            )
            emitter.emit(
                ev.PHASE_SESSION_CREATE,
                ev.STATUS_OK,
                "",
                {
                    "session": cfg.session,
                    "accelerator": accelerator_type,
                    "variant": cfg.accelerator_spec,
                },
            )
    except (TaskError, TransportError) as error:
        _emit_failure(emitter, cfg, error, clean=True, session_kept=False)
        raise
    except Exception as error:
        _emit_failure(emitter, cfg, error, clean=False, session_kept=False)
        raise

    # ── 执行段（旧 main() try/finally 边界的精确对应）──
    workflow_started = False
    workflow_completed = False
    output_retrieved = False
    output_zip = out_dir / "output.zip"
    failure: Exception | None = None
    failure_clean = False
    keep_vm = False
    try:
        if cfg.mount_drive and not cfg.reuse:
            emitter.emit(
                ev.PHASE_DRIVE_MOUNT,
                ev.STATUS_START,
                ">>> 正在挂载 Google Drive (code/backup/models)...",
                {"session": cfg.session},
            )
            drive_mounted = transport.mount_drive(cfg.session)
            emitter.emit(
                ev.PHASE_DRIVE_MOUNT,
                ev.STATUS_OK if drive_mounted else ev.STATUS_WARN,
                "" if drive_mounted else (
                    ">>> Google Drive 挂载失败，将使用 VM 本地模型缓存"
                ),
                {"session": cfg.session, "mounted": drive_mounted},
            )
        elif cfg.mount_drive:
            emitter.emit(
                ev.PHASE_DRIVE_MOUNT,
                ev.STATUS_OK,
                ">>> 复用模式: 仅检查已有 Google Drive 挂载，不重新挂载或认证",
                {"session": cfg.session},
            )

        emitter.emit(
            ev.PHASE_INPUT_UPLOAD,
            ev.STATUS_START,
            "",
            {"count": len(staged_inputs) + 2, "session": cfg.session},
        )
        run_config = {
            "backend": cfg.backend,
            "mount_drive": cfg.mount_drive,
            "model_mode": cfg.model_mode,
            "accelerator": accelerator_type,
            "input_files": [
                {
                    "workflow_path": item["workflow_path"],
                    "remote_path": item["remote_path"],
                }
                for item in staged_inputs
            ],
        }
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", encoding="utf-8") as config_file:
            json.dump(run_config, config_file, ensure_ascii=False)
            config_file.flush()
            _retrying(emitter, ev.PHASE_INPUT_UPLOAD, "上传运行配置", lambda: transport.upload(
                cfg.session, config_file.name, "/content/colab_run_config.json"))
        _retrying(emitter, ev.PHASE_INPUT_UPLOAD, "上传工作流配置", lambda: transport.upload(
            cfg.session, config_snapshot, "/content/workflow_config.json"))
        _retrying(emitter, ev.PHASE_INPUT_UPLOAD, "上传 API JSON", lambda: transport.upload(
            cfg.session, api_snapshot, "/content/api.json"))
        for item in staged_inputs:
            _retrying(emitter, ev.PHASE_INPUT_UPLOAD, "上传输入文件",
                      lambda item=item: transport.upload(
                          cfg.session, item["snapshot_path"], item["remote_path"]))
        emitter.emit(
            ev.PHASE_INPUT_UPLOAD,
            ev.STATUS_OK,
            "",
            {"count": len(staged_inputs) + 2, "session": cfg.session},
        )

        if cfg.reuse:
            emitter.emit(
                ev.PHASE_ENV_SETUP,
                ev.STATUS_OK,
                ">>> 复用模式: 跳过 ComfyUI 通用环境安装",
                {"session": cfg.session},
            )
        else:
            emitter.emit(
                ev.PHASE_ENV_SETUP,
                ev.STATUS_START,
                ">>> 安装/检查 ComfyUI 通用环境",
                {"session": cfg.session},
            )
            # 环境安装脚本幂等（依赖就绪标记文件），可安全重放
            _retrying(emitter, ev.PHASE_ENV_SETUP, "环境安装", lambda: transport.exec_script(
                cfg.session, REMOTE_SETUP, cfg.timeout, _sink(emitter)))
            emitter.emit(ev.PHASE_ENV_SETUP, ev.STATUS_OK, "",
                         {"session": cfg.session})

        emitter.emit(
            ev.PHASE_WORKFLOW_EXEC,
            ev.STATUS_START,
            f">>> 执行工作流: {workflow_name}",
            {"session": cfg.session, "workflow_name": workflow_name},
        )
        workflow_started = True
        try:
            output_streamed = transport.exec_workflow(
                cfg.session, REMOTE_WORKFLOW, cfg.timeout, output_zip, _sink(emitter)
            )
        except Exception as error:
            if isinstance(error, WorkflowNotSubmittedError):
                workflow_started = False
                raise
            # 执行阶段断线：不重放工作流（会重复计费、覆盖产物），
            # 而是探测远端会话与产物清单，远端跑完后改走下载通道
            if not is_transient_error(error) or not _reattach_after_disconnect(
                    transport, cfg, emitter, error):
                raise
            output_streamed = False
        workflow_completed = True
        emitter.emit(ev.PHASE_WORKFLOW_EXEC, ev.STATUS_OK, "",
                     {"session": cfg.session})
        if not output_streamed:
            emitter.emit(
                ev.PHASE_OUTPUT_TRANSFER,
                ev.STATUS_WARN,
                ">>> 执行通道未包含输出数据，改用 Contents API 兼容下载...",
                {"reason": "stream_absent", "fallback": "cli"},
            )
            _retrying(emitter, ev.PHASE_OUTPUT_TRANSFER, "产物下载",
                      lambda: transport.download_output(
                          cfg.session, output_zip, _sink(emitter)))
        output_retrieved = True
        size_bytes = output_zip.stat().st_size
        sha256 = file_sha256(output_zip)
        emitter.emit(
            ev.PHASE_OUTPUT_TRANSFER,
            ev.STATUS_OK,
            f"结果已下载到: {output_zip}",
            {
                "local_path": str(output_zip),
                "size_bytes": size_bytes,
                "sha256": sha256,
                "verified": True,
            },
        )
    except (TaskError, TransportError) as error:
        failure = error
        failure_clean = True
    except Exception as error:
        failure = error
    finally:
        retrieval_failed = workflow_started and not output_retrieved
        keep_vm = cfg.keep or cfg.reuse or retrieval_failed
        if keep_vm:
            if retrieval_failed:
                if workflow_completed:
                    emitter.emit(
                        ev.PHASE_SESSION_KEEP,
                        ev.STATUS_WARN,
                        f">>> 工作流已经完成，但结果取回失败；为保护远端成品，"
                        f"不会自动停止会话: {cfg.session}",
                        {"session": cfg.session, "reason": "retrieval_failed"},
                    )
                else:
                    emitter.emit(
                        ev.PHASE_SESSION_KEEP,
                        ev.STATUS_WARN,
                        f">>> 工作流执行通道异常；为保留诊断现场和可能已生成的"
                        f"结果，不会自动停止会话: {cfg.session}",
                        {"session": cfg.session, "reason": "channel_abnormal"},
                    )
                emitter.emit(
                    ev.PHASE_SESSION_KEEP,
                    ev.STATUS_WARN,
                    f">>> 可稍后重试: {_recovery_hint(cfg)}",
                    {"session": cfg.session,
                     "recovery_hint": _recovery_hint(cfg)},
                )
            else:
                emitter.emit(
                    ev.PHASE_SESSION_KEEP,
                    ev.STATUS_OK,
                    f">>> 会话已保留，可继续提交任务: {cfg.session}",
                    {"session": cfg.session},
                )
        else:
            emitter.emit(ev.PHASE_SESSION_STOP, ev.STATUS_START, "",
                         {"session": cfg.session})
            transport.stop_session(cfg.session)
            emitter.emit(ev.PHASE_SESSION_STOP, ev.STATUS_OK, "",
                         {"session": cfg.session})

    if failure is not None:
        _emit_failure(
            emitter, cfg, failure, clean=failure_clean, session_kept=keep_vm
        )
        raise failure

    duration_ms = round((time.monotonic() - started_monotonic) * 1000)
    result = TaskResult(
        task_id=task_id,
        status="succeeded",
        output_zip=output_zip,
        output_size_bytes=size_bytes,
        output_sha256=sha256,
        session_kept=keep_vm,
        transport_mode=transport.mode,
        duration_ms=duration_ms,
    )
    emitter.emit(
        ev.PHASE_TASK_DONE,
        ev.STATUS_OK,
        "",
        {
            "task_id": task_id,
            "output_zip": str(output_zip),
            "size_bytes": size_bytes,
            "sha256": sha256,
            "session_kept": keep_vm,
            "transport": transport.mode,
            "duration_ms": duration_ms,
        },
    )
    return result


def _retrying(emitter: EventEmitter, phase: str, step: str, fn):
    """幂等步骤的瞬时故障退避重试；每次重试发一条 warn 事件。"""

    def on_retry(attempt, total, delay, error):
        emitter.emit(
            phase,
            ev.STATUS_WARN,
            f">>> {step}遇到瞬时故障，{delay:.0f} 秒后重试 ({attempt + 1}/{total}): {error}",
            {"reason": "transient_retry", "step": step, "attempt": attempt + 1,
             "max_attempts": total, "delay_seconds": delay,
             "error_type": type(error).__name__},
        )

    return retry_transient(fn, step=step, on_retry=on_retry)


def _session_alive(transport, session: str) -> bool:
    """会话是否仍可读（ls /content 成功）；不可读 = VM 已消失或连接仍未恢复。"""
    try:
        transport.check_session_readable(session)
    except TransportError:
        return False
    return True


def _reattach_after_disconnect(transport, cfg: TaskConfig, emitter: EventEmitter,
                               error: BaseException) -> bool:
    """执行阶段连接断开后尝试重连远端进程；True = 远端已完成，可下载产物。"""
    emitter.emit(
        ev.PHASE_WORKFLOW_EXEC,
        ev.STATUS_WARN,
        f">>> 执行阶段连接断开（{error}），探测远端进程并等待其完成...",
        {"reason": "reattach_start", "session": cfg.session,
         "error_type": type(error).__name__},
    )

    def on_wait(elapsed, limit):
        emitter.emit(
            ev.PHASE_WORKFLOW_EXEC,
            ev.STATUS_PROGRESS,
            f">>> 等待远端工作流完成（已等待 {elapsed:.0f}/{limit:.0f} 秒）",
            {"reason": "reattach_wait", "elapsed_seconds": round(elapsed)},
        )

    ok = reattach_workflow(
        session_alive=lambda: _session_alive(transport, cfg.session),
        output_ready=lambda: transport.remote_output_ready(cfg.session),
        on_wait=on_wait,
    )
    emitter.emit(
        ev.PHASE_WORKFLOW_EXEC,
        ev.STATUS_OK if ok else ev.STATUS_WARN,
        ">>> 已重新连接：远端工作流已完成，改用下载通道取回产物" if ok
        else ">>> 重连失败：远端会话不可用或等待超时",
        {"reason": "reattach_ok" if ok else "reattach_failed", "session": cfg.session},
    )
    return ok


def _sink(emitter: EventEmitter):
    """把 transport 层的 sink(phase, status, message, data) 桥接成事件。"""

    def sink(phase, status, message, data=None):
        emitter.emit(phase, status, message or "", data)

    return sink


def _recovery_hint(cfg: TaskConfig) -> str:
    if cfg.recovery_command:
        return cfg.recovery_command
    return "POST /tasks/{id}/redownload"


def _emit_failure(
    emitter: EventEmitter,
    cfg: TaskConfig,
    error: Exception,
    clean: bool,
    session_kept: bool,
) -> None:
    """失败收尾：可预期错误发阶段 error 事件，终态统一 task.failed。"""
    if clean:
        emitter.emit(
            emitter.current_phase or ev.PHASE_TASK_START,
            ev.STATUS_ERROR,
            str(error),
        )
    emitter.emit(
        ev.PHASE_TASK_FAILED,
        ev.STATUS_ERROR,
        "",
        {
            "error_type": type(error).__name__,
            "message": str(error),
            "session_kept": session_kept,
            "recovery_hint": _recovery_hint(cfg),
        },
    )


# ── dry_run：完整事件流程演练，不建 VM、不下模型、不调 colab ──

_DRY_STEP_SECONDS = 0.1


def _run_dry(
    cfg: TaskConfig,
    emitter: EventEmitter,
    task_id: str,
    started_monotonic: float,
) -> TaskResult:
    """按真实节奏走完 emit 流程，用于事件协议与前端联调。"""
    workflow_config_path = cfg.workflow_config
    workflow_config = load_json(workflow_config_path, "工作流配置")
    api_json = resolve_api_json(cfg.api_json, workflow_config_path, workflow_config)
    workflow = load_json(api_json, "API 工作流")
    _apply_workflow_overrides(
        workflow,
        workflow_config.get("editable_fields", []),
        cfg.workflow_overrides,
    )
    frame_inputs_changed = apply_optional_last_frame(workflow, workflow_config, cfg.use_last_frame)
    input_dir_overridden = cfg.input_dir is not None
    input_dir = cfg.input_dir or (HERE / "image")
    input_references = collect_input_references(
        workflow, workflow_config.get("input_fields", []))
    search_dirs = build_search_dirs(input_dir, api_json)
    resolved_inputs = resolve_input_files(
        input_references, search_dirs, workflow_config_path, input_dir_overridden
    )

    staged_inputs = []
    for index, (reference, local_path) in enumerate(resolved_inputs.items()):
        staged_inputs.append({
            "workflow_path": reference,
            "local_path": local_path,
            "remote_path": f"/content/workflow_input_{index:03d}{Path(reference).suffix}",
        })

    workflow_name = workflow_config.get("name", workflow_config_path.stem)
    emitter.emit(
        ev.PHASE_TASK_START,
        ev.STATUS_START,
        f"工作流: {workflow_name}",
        {
            "workflow_name": workflow_name,
            "session": cfg.session,
            "accelerator": cfg.accelerator_kind,
            "variant": cfg.accelerator_spec,
            "dry_run": True,
        },
    )
    emitter.emit(ev.PHASE_INPUT_RESOLVE, ev.STATUS_START, f"API JSON: {api_json}")
    emitter.emit(
        ev.PHASE_INPUT_RESOLVE,
        ev.STATUS_OK,
        f"输入文件: { {key: str(value) for key, value in resolved_inputs.items()} }",
        {
            "files": [
                {
                    "workflow_path": item["workflow_path"],
                    "local_path": str(item["local_path"]),
                    "remote_path": item["remote_path"],
                }
                for item in staged_inputs
            ]
        },
    )
    emitter.emit(ev.PHASE_TASK_START, ev.STATUS_OK, f"任务编号: {task_id}",
                 {"task_id": task_id})

    out_dir = cfg.output_dir or (default_output_root() / task_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    _snapshot_api_json(
        api_json,
        out_dir / "api.json",
        workflow,
        bool(cfg.workflow_overrides) or frame_inputs_changed,
    )
    shutil.copyfile(workflow_config_path, out_dir / "workflow.json")
    _snapshot_inputs(out_dir, staged_inputs)

    validate_workflow_accelerator(workflow_config, cfg.accelerator_kind)

    sleep = time.sleep
    if cfg.reuse:
        emitter.emit(ev.PHASE_SESSION_REUSE, ev.STATUS_START,
                     f">>> 检查并复用现有会话: {cfg.session}",
                     {"session": cfg.session})
        sleep(_DRY_STEP_SECONDS)
        emitter.emit(ev.PHASE_SESSION_REUSE, ev.STATUS_OK,
                     f">>> 现有会话可用: {cfg.session}",
                     {"session": cfg.session, "accelerator": cfg.accelerator_kind})
    else:
        emitter.emit(ev.PHASE_SESSION_CREATE, ev.STATUS_START, "",
                     {"session": cfg.session,
                      "accelerator": cfg.accelerator_kind,
                      "variant": cfg.accelerator_spec})
        sleep(_DRY_STEP_SECONDS)
        emitter.emit(ev.PHASE_SESSION_CREATE, ev.STATUS_OK, "",
                     {"session": cfg.session,
                      "accelerator": cfg.accelerator_kind,
                      "variant": cfg.accelerator_spec})

    if cfg.mount_drive:
        emitter.emit(ev.PHASE_DRIVE_MOUNT, ev.STATUS_START,
                     ">>> 正在挂载 Google Drive (code/backup/models)...",
                     {"session": cfg.session})
        sleep(_DRY_STEP_SECONDS)
        emitter.emit(ev.PHASE_DRIVE_MOUNT, ev.STATUS_OK, "",
                     {"session": cfg.session})

    emitter.emit(ev.PHASE_INPUT_UPLOAD, ev.STATUS_START, "",
                 {"count": len(staged_inputs) + 2, "session": cfg.session})
    for item in staged_inputs:
        emitter.emit(
            ev.PHASE_INPUT_UPLOAD,
            ev.STATUS_PROGRESS,
            f"+ colab upload -s {cfg.session} {item['local_path']} "
            f"{item['remote_path']}",
            {"session": cfg.session, "remote_path": item["remote_path"]},
        )
        sleep(_DRY_STEP_SECONDS)
    emitter.emit(ev.PHASE_INPUT_UPLOAD, ev.STATUS_OK, "",
                 {"count": len(staged_inputs) + 2, "session": cfg.session})

    if not cfg.reuse:
        emitter.emit(ev.PHASE_ENV_SETUP, ev.STATUS_START,
                     ">>> 安装/检查 ComfyUI 通用环境", {"session": cfg.session})
        sleep(_DRY_STEP_SECONDS)
        emitter.emit(ev.PHASE_ENV_SETUP, ev.STATUS_OK, "", {"session": cfg.session})

    custom_nodes = workflow_config.get("custom_nodes", []) or []
    emitter.emit(ev.PHASE_NODE_PREPARE, ev.STATUS_START,
                 f">>> [1/4] 准备第三方节点 ({len(custom_nodes)} 个)",
                 {"total": len(custom_nodes)})
    sleep(_DRY_STEP_SECONDS)
    emitter.emit(ev.PHASE_NODE_PREPARE, ev.STATUS_OK, "",
                 {"total": len(custom_nodes)})

    models = workflow_config.get("models", []) or []
    total_bytes = sum(int(m.get("size_bytes", 0)) for m in models)
    emitter.emit(ev.PHASE_MODEL_PREPARE, ev.STATUS_START,
                 f">>> [2/4] 准备模型 ({len(models)} 个)",
                 {"total": len(models), "total_bytes": total_bytes})
    if cfg.model_mode == "download" or not cfg.mount_drive:
        model_source, model_action = "huggingface", "download"
    elif cfg.model_mode == "symlink":
        model_source, model_action = "drive_cache", "symlink"
    elif cfg.model_mode == "copy":
        model_source, model_action = "drive_cache", "copy"
    elif cfg.backend == "colab":
        model_source, model_action = "drive_cache", "copy"
    else:
        model_source, model_action = "drive_cache", "symlink"
    for index, model in enumerate(models, start=1):
        size = int(model.get("size_bytes", 0))
        for done_fraction in (0.4, 0.8, 1.0):
            emitter.emit(
                ev.PHASE_MODEL_PREPARE,
                ev.STATUS_PROGRESS,
                f">>> [dry-run] 模型 {index}/{len(models)}: "
                f"{model.get('filename', '?')}",
                {
                    "index": index,
                    "total": len(models),
                    "repo": model.get("repo", ""),
                    "filename": model.get("filename", ""),
                    "size_bytes": size,
                    "source": model_source,
                    "action": model_action,
                    "done_bytes": int(size * done_fraction),
                },
            )
            sleep(_DRY_STEP_SECONDS)
    emitter.emit(ev.PHASE_MODEL_PREPARE, ev.STATUS_OK, "",
                 {"total": len(models), "total_bytes": total_bytes})

    emitter.emit(ev.PHASE_COMFY_START, ev.STATUS_START,
                 ">>> [4/4] 启动 ComfyUI 并运行工作流", {})
    sleep(_DRY_STEP_SECONDS)
    emitter.emit(ev.PHASE_COMFY_START, ev.STATUS_OK, "", {})

    emitter.emit(ev.PHASE_WORKFLOW_EXEC, ev.STATUS_START,
                 f">>> 执行工作流: {workflow_name}",
                 {"session": cfg.session, "workflow_name": workflow_name})
    total_nodes = max(1, len(workflow))
    node_labels = [
        f"{node_id}: {node.get('class_type', '?')}"
        for node_id, node in list(workflow.items())[:5]
    ] or ["1: KSampler"]
    for completed, label in enumerate(node_labels):
        for step in (1, 2):
            percent = round((completed + step / 2) / len(node_labels) * 100, 1)
            emitter.emit(
                ev.PHASE_WORKFLOW_EXEC,
                ev.STATUS_PROGRESS,
                f"工作流 [{'#' * completed}{'-' * (len(node_labels) - completed)}] "
                f"{completed}/{len(node_labels)} | 当前 {label}",
                {
                    "completed_nodes": completed,
                    "total_nodes": total_nodes,
                    "current_node": label,
                    "step": step,
                    "steps": 2,
                    "percent": percent,
                },
            )
            emitter.emit(
                ev.PHASE_METRICS_SAMPLE,
                ev.STATUS_PROGRESS,
                "",
                {
                    "gpu_util": 90 + step,
                    "gpu_util_avg30": 85.0,
                    "gpu_util_peak": 100,
                    "vram_used_mb": 38210,
                    "vram_total_mb": 40960,
                    "ram_used_mb": 41230,
                    "cpu_percent": 62,
                },
            )
            sleep(_DRY_STEP_SECONDS)
    emitter.log("✅ 工作流执行完成! (dry-run)")
    emitter.emit(ev.PHASE_WORKFLOW_EXEC, ev.STATUS_OK, "", {"session": cfg.session})

    # 产出真实可校验的 output.zip，供下载链路联调
    output_zip = out_dir / "output.zip"
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "dry_run/result.txt",
            f"dry-run 任务 {task_id} 于 {ev.utc_now_iso()} 生成\n",
        )
    archive_bytes = output_zip.read_bytes()
    archive_size = len(archive_bytes)
    archive_sha256 = file_sha256(output_zip)
    emitter.emit(
        ev.PHASE_OUTPUT_PACK,
        ev.STATUS_OK,
        f">>> 完成, 输出包 {archive_size / 1024**2:.1f} MiB，已准备 1 个下载分块 (dry-run)",
        {"archive_size": archive_size, "archive_sha256": archive_sha256, "parts": 1},
    )
    emitter.emit(
        ev.PHASE_OUTPUT_TRANSFER,
        ev.STATUS_START,
        f">>> 开始取回输出: {archive_size / 1024**2:.1f} MiB，共 1 个分块",
        {"archive_size": archive_size, "archive_sha256": archive_sha256,
         "parts": 1},
    )
    sleep(_DRY_STEP_SECONDS)
    emitter.emit(
        ev.PHASE_OUTPUT_TRANSFER,
        ev.STATUS_PROGRESS,
        ">>> 下载分块 1/1: part-00000.bin",
        {"part": 1, "parts": 1, "part_name": "part-00000.bin",
         "transport": "native"},
    )
    emitter.emit(
        ev.PHASE_OUTPUT_TRANSFER,
        ev.STATUS_OK,
        f"结果已下载到: {output_zip}",
        {
            "local_path": str(output_zip),
            "size_bytes": archive_size,
            "sha256": archive_sha256,
            "verified": True,
        },
    )

    keep_vm = cfg.keep or cfg.reuse
    if keep_vm:
        emitter.emit(ev.PHASE_SESSION_KEEP, ev.STATUS_OK,
                     f">>> 会话已保留，可继续提交任务: {cfg.session}",
                     {"session": cfg.session})
    else:
        emitter.emit(ev.PHASE_SESSION_STOP, ev.STATUS_START, "",
                     {"session": cfg.session})
        emitter.emit(ev.PHASE_SESSION_STOP, ev.STATUS_OK, "",
                     {"session": cfg.session})

    duration_ms = round((time.monotonic() - started_monotonic) * 1000)
    emitter.emit(
        ev.PHASE_TASK_DONE,
        ev.STATUS_OK,
        "",
        {
            "task_id": task_id,
            "output_zip": str(output_zip),
            "size_bytes": archive_size,
            "sha256": archive_sha256,
            "session_kept": keep_vm,
            "transport": "dry_run",
            "duration_ms": duration_ms,
        },
    )
    return TaskResult(
        task_id=task_id,
        status="succeeded",
        output_zip=output_zip,
        output_size_bytes=archive_size,
        output_sha256=archive_sha256,
        session_kept=keep_vm,
        transport_mode="dry_run",
        duration_ms=duration_ms,
    )
