"""FastAPI 应用：Runner 的 HTTP 边界（doc/02 §2）。

安全基线（doc/01 §7）：全部请求需 `X-Internal-Token` 共享密钥；
服务只绑定 127.0.0.1（见 __main__.py），与浏览器 JWT 是两套独立体系。

阻塞纪律：任务线程在 ThreadPoolExecutor（manager.py）；涉及 subprocess /
文件遍历的路由一律用同步 `def`（FastAPI 自动放线程池），SSE 用同步生成器
（Starlette 放线程池迭代）—— asyncio 事件循环里没有任何阻塞调用。
"""
import hmac
import json
import os
import queue
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from colab_core import events as ev
from colab_core.sessions import (
    create_named_session,
    stop_named_session,
    stop_unmapped_endpoint,
)
from colab_transport import (
    colab_cli_version,
    probe_kernel_client_api,
    probe_private_api,
)

from . import __version__
from . import sessions as sessions_service
from . import workflows as workflows_service
from .manager import ManagerError, TaskManager
from .store import read_events_file
from .workflows import WorkflowError

SSE_KEEPALIVE_SECONDS = 15  # doc/02 §2.5：每 15 秒一行 `: keepalive`


def error_body(code: str, message: str, detail: Optional[dict] = None) -> dict:
    """doc/02 §3.4 统一错误格式。"""
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


class InternalTokenMiddleware:
    """X-Internal-Token 校验（纯 ASGI，不包装响应体，不干扰 SSE 流）。"""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"x-internal-token", b"").decode("utf-8", "ignore")
        if not self._token or not hmac.compare_digest(provided, self._token):
            response = JSONResponse(
                status_code=401,
                content=error_body(
                    "UNAUTHORIZED", "缺少或错误的 X-Internal-Token"),
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _sse_frame(event: dict) -> str:
    return (
        f"id: {event['seq']}\n"
        f"event: task\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )


def create_app(
    token: Optional[str] = None,
    output_root: Optional[Path] = None,
    max_workers: Optional[int] = None,
) -> FastAPI:
    """应用工厂。token 缺省读环境变量 COLAB_RUNNER_TOKEN；都没有则拒绝启动。"""
    resolved_token = token if token is not None else os.environ.get(
        "COLAB_RUNNER_TOKEN")
    if not resolved_token:
        raise RuntimeError(
            "缺少共享密钥：请设置环境变量 COLAB_RUNNER_TOKEN 后再启动 Runner"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = TaskManager(
            output_root=output_root,
            max_workers=max_workers
            or int(os.environ.get("COLAB_RUNNER_WORKERS", "4")),
        )
        app.state.manager.recover()  # runner_state.json 崩溃恢复（doc/03 §1）
        yield
        app.state.manager.shutdown()

    app = FastAPI(title="colab-runner", version=__version__, lifespan=lifespan)
    app.add_middleware(InternalTokenMiddleware, token=resolved_token)

    def manager(request: Request) -> TaskManager:
        return request.app.state.manager

    @app.exception_handler(ManagerError)
    async def manager_error_handler(_request, error: ManagerError):
        return JSONResponse(
            status_code=error.http_status,
            content=error_body(error.code, str(error), error.detail),
        )

    @app.exception_handler(WorkflowError)
    async def workflow_error_handler(_request, error: WorkflowError):
        return JSONResponse(
            status_code=error.http_status,
            content=error_body(error.code, str(error), error.detail),
        )

    # ── 2.1 健康与自检 ───────────────────────────────────────

    @app.get("/health")
    def health(request: Request):
        probe = probe_private_api()
        kernel_dependency_ok = probe_kernel_client_api()
        probe["kernel_client"] = probe["kernel_client"] and kernel_dependency_ok
        requested = (os.environ.get("COLAB_TRANSPORT") or "native").lower()
        # 与 get_transport() 的降级语义一致：探测失败 → 实际走 cli
        mode = "cli" if requested == "cli" or not all(probe.values()) else "native"
        mgr = manager(request)
        body = {
            "ok": kernel_dependency_ok,
            "version": __version__,
            "colab_cli_version": colab_cli_version(),
            "transport": {
                "mode": mode,
                "requested": requested,
                "degraded": requested != mode,
                "probe": probe,
            },
            "running_tasks": mgr.running_count(),
            "recovered_inflight": list(mgr.recovered_inflight),
        }
        if not kernel_dependency_ok:
            body.update(error_body(
                "KERNEL_CLIENT_INCOMPATIBLE",
                "Colab 内核依赖不兼容，请按 requirements.txt 重新安装依赖并重启 Runner",
            ))
            return JSONResponse(status_code=503, content=body)
        return body

    # ── 2.3 任务 ─────────────────────────────────────────────

    @app.post("/tasks", status_code=202)
    def submit_task(request: Request, payload: dict = Body(...)):
        record = manager(request).submit(payload)
        return {"task_id": record.task_id, "status": "queued"}

    @app.get("/tasks/{task_id}")
    def task_snapshot(request: Request, task_id: str):
        return manager(request).snapshot(manager(request).get(task_id))

    @app.get("/tasks/{task_id}/events")
    def task_events(
        request: Request,
        task_id: str,
        from_seq: int = Query(default=-1, ge=-1),
    ):
        return events_response(manager(request), task_id, from_seq)

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(
        request: Request,
        task_id: str,
        payload: Optional[dict] = Body(None),
    ):
        """interrupt（默认，restart-kernel，VM 保留可重投）/ destroy（stop）。"""
        mode = (payload or {}).get("mode", "interrupt")
        outcome = manager(request).cancel(task_id, mode)
        if outcome == "canceled":
            return {"task_id": task_id, "status": "canceled", "mode": mode}
        return JSONResponse(
            status_code=202,
            content={"task_id": task_id, "status": "canceling", "mode": mode},
        )

    @app.post("/tasks/{task_id}/drive-confirm")
    def confirm_task_drive(request: Request, task_id: str):
        session = manager(request).confirm_drive(task_id)
        return {
            "task_id": task_id,
            "status": "confirmed",
            "session": session,
        }

    @app.post("/tasks/{task_id}/redownload", status_code=202)
    def redownload_task(request: Request, task_id: str):
        manager(request).redownload(task_id)
        return {"task_id": task_id, "status": "redownloading"}

    @app.get("/tasks/{task_id}/output")
    def task_output(request: Request, task_id: str):
        """返回本地绝对路径 JSON（doc/01 §6：大 ZIP 不走 HTTP 中转）。"""
        record = manager(request).get(task_id)
        if record.output_zip is None:
            return JSONResponse(
                status_code=409,
                content=error_body(
                    "OUTPUT_NOT_READY",
                    f"任务尚无输出产物: {task_id}（状态 {record.status}）",
                ),
            )
        if not record.output_zip.is_file():
            return JSONResponse(
                status_code=404,
                content=error_body(
                    "OUTPUT_FILE_MISSING",
                    f"输出文件已不存在: {record.output_zip}",
                ),
            )
        return {
            "task_id": task_id,
            "path": str(record.output_zip),
            "size_bytes": record.output_size_bytes,
            "sha256": record.output_sha256,
        }

    # ── 2.2 工作流 ───────────────────────────────────────────

    @app.get("/workflows")
    def list_workflows():
        return workflows_service.list_workflows()

    @app.post("/workflows/{slug}/frames")
    def set_workflow_frames(slug: str, payload: dict = Body(...)):
        return workflows_service.set_frames(
            slug, payload.get("first"), payload.get("last"))

    # ── 2.4 会话 ─────────────────────────────────────────────

    @app.get("/sessions")
    def list_sessions(request: Request):
        mgr = manager(request)
        try:
            entries = sessions_service.list_sessions()
        except sessions_service.SessionQueryError as error:
            return JSONResponse(status_code=503,
                                content=error_body("SESSION_QUERY_FAILED", str(error)))
        for entry in entries:
            task_id = mgr.task_id_for_session(entry.get("name") or "")
            if task_id is not None:
                entry["task_id"] = task_id
        return entries

    @app.post("/sessions", status_code=201)
    def create_session(request: Request, payload: Optional[dict] = Body(None)):
        """前台启动 Colab VM（colab new）。body: name + accelerator + mount_drive。"""
        body = payload or {}
        name = body.get("name") or body.get("session")
        accelerator = body.get("accelerator") if isinstance(body.get("accelerator"), dict) else {}
        kind = accelerator.get("kind", "gpu")
        spec = accelerator.get("spec", "T4")
        mount_drive = bool(body.get("mount_drive", False))
        if not isinstance(name, str) or not name:
            return JSONResponse(
                status_code=400,
                content=error_body("BAD_REQUEST", "缺少会话名 name"),
            )
        mgr = manager(request)
        owner = mgr.task_id_for_session(name)
        if owner is not None:
            return JSONResponse(
                status_code=409,
                content=error_body(
                    "SESSION_BUSY",
                    f"会话 {name} 仍有任务运行",
                    {"session": name, "running_task_id": owner},
                ),
            )
        try:
            result = create_named_session(name, kind, spec, mount_drive)
            sessions_service.invalidate_sessions()
            return result
        except ValueError as error:
            return JSONResponse(
                status_code=400,
                content=error_body("BAD_REQUEST", str(error)),
            )
        except RuntimeError as error:
            return JSONResponse(
                status_code=500,
                content=error_body("SESSION_CREATE_FAILED", str(error)),
            )

    # 先声明 by-endpoint，避免被 /sessions/{name} 吃掉；endpoint 含特殊字符，
    # 用 :path 转换器承接
    @app.delete("/sessions/by-endpoint/{endpoint:path}")
    def delete_session_by_endpoint(endpoint: str):
        try:
            stop_unmapped_endpoint(endpoint)
            sessions_service.invalidate_sessions()
        except ValueError as error:
            return JSONResponse(
                status_code=400,
                content=error_body("BAD_REQUEST", str(error)),
            )
        except RuntimeError as error:
            return JSONResponse(
                status_code=500,
                content=error_body("SESSION_STOP_FAILED", str(error)),
            )
        return {"ok": True, "endpoint": endpoint}

    @app.delete("/sessions/{name}")
    def delete_session(request: Request, name: str):
        try:
            log = stop_named_session(name)
        except RuntimeError as error:
            return JSONResponse(
                status_code=500,
                content=error_body("SESSION_STOP_FAILED", str(error)),
            )
        # 会话已确认停止：解除 Runner 重启恢复时挂在其上的对账锁
        manager(request).release_recovered_session(name)
        sessions_service.invalidate_sessions()
        return {"ok": True, "name": name, "log": log}

    return app


def events_response(mgr: TaskManager, task_id: str, from_seq: int):
    """SSE 响应：seq > from_seq 的事件；终态后流关闭；每 15 秒 keepalive。

    在线任务：回放（ring/文件）+ 订阅在线队列；已结束任务：回放
    events.jsonl 后立即关闭。事件必须落盘不能只放内存（doc/02 §2.5）。
    """
    generator = events_generator(mgr, task_id, from_seq)
    if generator is None:
        return JSONResponse(
            status_code=404,
            content=error_body("TASK_NOT_FOUND", f"任务不存在: {task_id}"),
        )
    return _sse_response(generator)


def events_generator(mgr: TaskManager, task_id: str, from_seq: int):
    """SSE 文本生成器；任务不存在（且无磁盘记录）返回 None。

    独立出来是为了测试能直接驱动同步生成器 —— StreamingResponse 会把它包成
    async 迭代器，TestClient 又会等流读完才返回，都无法与流中途交互。
    """
    record = mgr.peek(task_id)
    live = (
        record is not None
        and record.event_log is not None
        and not record.terminal
        and record.status in ("queued", "running", "redownloading")
    )
    if live:
        event_log = record.event_log
        replay, high_water, subscriber = event_log.subscribe(from_seq)

        def live_stream():
            try:
                for event in replay:
                    yield _sse_frame(event)
                    if (event["phase"] in ev.TERMINAL_PHASES
                            and record.terminal
                            and event["seq"] == record.last_seq):
                        return  # 只接受当前终态；redownload 前的失败仅是历史
                while True:
                    try:
                        item = subscriber.get(timeout=SSE_KEEPALIVE_SECONDS)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        return
                    if item["seq"] <= high_water:
                        continue  # 注册前的事件，回放已覆盖（对落盘集）
                    yield _sse_frame(item)
                    if item["phase"] in ev.TERMINAL_PHASES:
                        return
            finally:
                event_log.unsubscribe(subscriber)

        return live_stream()

    # 终态 / 对账项 / 磁盘上的老任务：回放后立即关闭
    if record is not None and record.event_log is not None:
        events = record.event_log.replay(from_seq)
    else:
        events_path = mgr.output_root / task_id / "events.jsonl"
        if not events_path.is_file():
            return None
        events = read_events_file(events_path, from_seq)

    def replay_stream():
        for event in events:
            yield _sse_frame(event)

    return replay_stream()


def _sse_response(generator) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
