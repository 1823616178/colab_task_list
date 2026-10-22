"""kllm 网关: 把 Kaggle 模型代理包装成本地 OpenAI 兼容端点。

对外只暴露最小面: /v1/models 和 /v1/chat/completions, 请求体原样透传。
可靠性策略:
- 上游 401/403: 续期 token 后重试一次;
- 上游 429/5xx: 按 Retry-After 或指数退避重试, 最多 settings.max_retries 次;
- 网络错误(连接失败/超时): 同样重试, 最终返回 502 而不是 500;
- 上游返回非 JSON: 原样透传字节, 不崩溃。
每个请求带 X-Request-ID(客户端给了就沿用), 日志记录模型、状态码与耗时。
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable

import anyio
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..common.config import AVAILABLE_MODELS, Settings, load_settings
from ..common.errors import CredentialError
from ..common.logging import get_logger, request_id_var
from ..common.retry import RetryPolicy, is_retryable_status
from .credentials import TokenManager
from .usage import append_record, extract_record

logger = get_logger(__name__)

# 上游认为 token 无效的状态码, 触发一次续期重试。
TOKEN_REJECTED_STATUSES = (401, 403)
REQUEST_ID_HEADER = "X-Request-ID"

Sleeper = Callable[[float], Awaitable[None]]


def create_app(settings: Settings | None = None,
               token_manager: TokenManager | None = None,
               http_client: httpx.AsyncClient | None = None,
               sleep: Sleeper | None = None) -> FastAPI:
    """构造网关应用。参数可注入, 便于测试(sleep 用于跳过真实退避等待)。"""
    settings = settings or load_settings()
    token_manager = token_manager or TokenManager(settings)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
    policy = RetryPolicy(
        max_retries=settings.max_retries,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    sleeper: Sleeper = sleep or anyio.sleep

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        """进程退出时关掉自己创建的 http 客户端。"""
        yield
        if owns_client:
            await client.aclose()

    app = FastAPI(title="kllm", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.token_manager = token_manager
    app.state.http_client = client

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """分配 request id, 记录耗时。流式响应的耗时只算到响应头发出。"""
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("%s %s 未处理异常", request.method, request.url.path)
            raise
        finally:
            request_id_var.reset(token)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "%s %s -> %s %.0fms [%s]", request.method, request.url.path,
            response.status_code, elapsed_ms, request_id,
        )
        return response

    def check_local_key(authorization: str | None) -> None:
        """校验本地访问密钥(未设置 KLLM_LOCAL_KEY 时不校验)。"""
        if settings.local_key is None:
            return
        expected = f"Bearer {settings.local_key}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="本地密钥不正确")

    async def credentials(stale=None):
        """取一份活凭据; 凭据获取是阻塞 IO, 放线程里做。

        传入 stale 表示手上那份被上游拒绝了, 需要强制续期。
        """
        try:
            if stale is not None:
                return await anyio.to_thread.run_sync(token_manager.force_refresh, stale)
            return await anyio.to_thread.run_sync(token_manager.get)
        except CredentialError as error:
            logger.error("获取凭据失败: %s", error)
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/healthz")
    async def healthz() -> dict:
        """存活探针。"""
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)) -> dict:
        """返回可用模型列表。

        上游没有 GET /models(实测 404), 这里用 Kaggle CLI 内置的静态列表。
        """
        check_local_key(authorization)
        return {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "owned_by": "kaggle-model-proxy"}
                for model in AVAILABLE_MODELS
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request,
                               authorization: str | None = Header(default=None)):
        """转发聊天补全请求, 支持流式与非流式。"""
        check_local_key(authorization)
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

        model = payload.get("model", "")
        streaming = bool(payload.get("stream"))
        logger.info("chat model=%s stream=%s", model, streaming)

        upstream = await _send_with_retry(payload, streaming)
        if streaming:
            return _streaming_response(upstream, model)
        return await _json_response(upstream, model)

    async def _send_with_retry(payload: dict, streaming: bool) -> httpx.Response:
        """发请求并按策略重试; 网络错误耗尽重试后返回 502。"""
        active = await credentials()
        token_refreshed = False
        attempt = 0
        while True:
            try:
                response = await _send_once(payload, streaming, active)
            except httpx.TransportError as error:
                if attempt >= policy.max_retries:
                    logger.error("上游网络错误, 已重试 %d 次: %s", attempt, error)
                    raise HTTPException(
                        status_code=502,
                        detail=f"上游网络错误({type(error).__name__}): {error}",
                    ) from error
                delay = policy.delay(attempt)
                logger.warning("上游网络错误 %s, %.1fs 后重试(%d/%d)",
                               type(error).__name__, delay, attempt + 1, policy.max_retries)
                attempt += 1
                await sleeper(delay)
                continue

            if response.status_code in TOKEN_REJECTED_STATUSES and not token_refreshed:
                await response.aclose()
                logger.info("上游拒绝 token(HTTP %s), 续期后重试", response.status_code)
                active = await credentials(stale=active)
                token_refreshed = True
                continue

            if is_retryable_status(response.status_code) and attempt < policy.max_retries:
                delay = policy.delay(attempt, response.headers.get("retry-after"))
                await response.aclose()
                logger.warning("上游 HTTP %s, %.1fs 后重试(%d/%d)",
                               response.status_code, delay, attempt + 1, policy.max_retries)
                attempt += 1
                await sleeper(delay)
                continue
            return response

    async def _send_once(payload: dict, streaming: bool, active) -> httpx.Response:
        """构造并发送一次上游请求, 流式时不读取响应体。"""
        request = client.build_request(
            "POST",
            f"{active.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {active.token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        return await client.send(request, stream=streaming)

    async def _json_response(upstream: httpx.Response, model: str) -> Response:
        """非流式: 读完响应, 记账, 原样返回; 非 JSON 响应按原字节透传。"""
        try:
            body = await upstream.aread()
        except httpx.TransportError as error:
            raise HTTPException(status_code=502, detail=f"读取上游响应失败: {error}") from error
        finally:
            await upstream.aclose()
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            logger.warning("上游返回非 JSON(HTTP %s, %d 字节), 原样透传",
                           upstream.status_code, len(body))
            return Response(
                content=body,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "text/plain"),
            )
        if upstream.status_code == 200 and isinstance(parsed, dict):
            append_record(settings.usage_path, extract_record(model, parsed, streamed=False))
        elif upstream.status_code != 200:
            logger.warning("上游返回 HTTP %s: %s", upstream.status_code, body[:300])
        return JSONResponse(content=parsed, status_code=upstream.status_code)

    def _streaming_response(upstream: httpx.Response, model: str) -> StreamingResponse:
        """流式: 字节级透传 SSE, 不解析重组, 保证 tool_calls 分片不失真。

        转发的字节一个不改; 只旁路复制一份用来抽取用量 —— 上游每个 chunk 都带
        累计 usage, 取最后一个见到的即可。
        """

        async def iterate():
            tail = b""
            last_usage: dict = {}
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                    if upstream.status_code == 200:
                        tail, last_usage = _scan_usage(tail + chunk, last_usage)
            except httpx.TransportError as error:
                # 响应头已经发出, 只能记录并结束流。
                logger.error("流式转发中断: %s", error)
            finally:
                await upstream.aclose()
                if upstream.status_code == 200:
                    append_record(
                        settings.usage_path,
                        extract_record(model, {"usage": last_usage}, streamed=True),
                    )

        media_type = upstream.headers.get("content-type", "text/event-stream")
        return StreamingResponse(
            iterate(),
            status_code=upstream.status_code,
            media_type=media_type,
        )

    return app


def _scan_usage(buffer: bytes, last_usage: dict) -> tuple[bytes, dict]:
    """从 SSE 缓冲里抽取最后一个 usage, 返回(未处理完的尾巴, 最新 usage)。"""
    while b"\n\n" in buffer:
        event, _, buffer = buffer.partition(b"\n\n")
        for line in event.split(b"\n"):
            if not line.startswith(b"data: ") or line == b"data: [DONE]":
                continue
            try:
                usage = json.loads(line[len(b"data: "):]).get("usage")
            except (ValueError, AttributeError):
                continue
            if usage:
                last_usage = usage
    return buffer, last_usage
