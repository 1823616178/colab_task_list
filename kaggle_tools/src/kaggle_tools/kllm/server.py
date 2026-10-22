"""kllm 网关: 把 Kaggle 模型代理包装成本地 OpenAI 兼容端点。

对外只暴露最小面: /v1/models 和 /v1/chat/completions, 请求体原样透传,
只负责换上一份活的 Kaggle token, 并在 401 时续期重试一次。
"""

import contextlib
import json

import anyio
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import AVAILABLE_MODELS, Settings, load_settings
from .credentials import CredentialError, TokenManager
from .usage import append_record, extract_record

# 上游认为 token 无效的状态码, 触发一次续期重试。
TOKEN_REJECTED_STATUSES = (401, 403)


def create_app(settings: Settings | None = None,
               token_manager: TokenManager | None = None,
               http_client: httpx.AsyncClient | None = None) -> FastAPI:
    """构造网关应用。参数可注入, 便于测试。"""
    settings = settings or load_settings()
    token_manager = token_manager or TokenManager(settings)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)

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

        model = payload.get("model", "")
        streaming = bool(payload.get("stream"))

        upstream = await _send_with_retry(payload, streaming)
        if streaming:
            return _streaming_response(upstream, model)
        return await _json_response(upstream, model)

    async def _send_with_retry(payload: dict, streaming: bool) -> httpx.Response:
        """发一次请求; 上游拒绝 token 时续期后只重试一次。"""
        active = await credentials()
        response = await _send_once(payload, streaming, active)
        if response.status_code in TOKEN_REJECTED_STATUSES:
            await response.aclose()
            refreshed = await credentials(stale=active)
            response = await _send_once(payload, streaming, refreshed)
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

    async def _json_response(upstream: httpx.Response, model: str) -> JSONResponse:
        """非流式: 读完响应, 记账, 原样返回。"""
        body = await upstream.aread()
        await upstream.aclose()
        if upstream.status_code == 200:
            try:
                append_record(
                    settings.usage_path,
                    extract_record(model, json.loads(body), streamed=False),
                )
            except ValueError:
                pass
        return JSONResponse(
            content=json.loads(body) if body else {},
            status_code=upstream.status_code,
        )

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
            except ValueError:
                continue
            if usage:
                last_usage = usage
    return buffer, last_usage
