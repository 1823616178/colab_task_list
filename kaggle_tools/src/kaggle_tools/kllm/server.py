"""kllm 网关: 把 Kaggle 模型代理包装成本地 OpenAI 兼容端点。

对外只暴露最小面: /v1/models 和 /v1/chat/completions, 请求体原样透传。
可靠性策略:
- 上游 401/403: 续期 token 后重试一次;
- 上游 429/503(可配置 KLLM_RETRY_STATUSES): 按 Retry-After 或指数退避重试;
  500/502 默认不重试, 因为上游可能已经处理并计费;
- 连接类网络错误(请求未送达)重试, 最终返回 502; 读超时等不重试, 直接 502;
- 首选模型仍失败(429/5xx/网络)时按 KLLM_FALLBACK_MODELS 依次换模型;
- 上游返回非 JSON: 原样透传字节, 不崩溃;
- 流式中途断开: 补发 SSE error 事件和 [DONE], 账本记为 incomplete。
额度: 达到 KLLM_DAILY_LIMIT / KLLM_MONTHLY_LIMIT 返回 429; 达到告警比例时加响应头并记日志。
并发: 同时转发给上游的请求数不超过 KLLM_MAX_CONCURRENCY, 多出的排队。
每个请求带 X-Request-ID(客户端给了就沿用), 日志记录模型、状态码与耗时。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
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
from ..common.retry import policy_from_settings
from .credentials import TokenManager
from .usage import Ledger, extract_record, ledger_from_settings

logger = get_logger(__name__)

# 上游认为 token 无效的状态码, 触发一次续期重试。
TOKEN_REJECTED_STATUSES = (401, 403)
REQUEST_ID_HEADER = "X-Request-ID"
MODEL_HEADER = "X-KLLM-Model"
BUDGET_HEADER = "X-KLLM-Budget"
BUDGET_WARNING_HEADER = "X-KLLM-Budget-Warning"
STREAM_DONE = b"data: [DONE]\n\n"


def should_fall_back(status_code: int) -> bool:
    """首选模型的哪些最终结果值得换备用模型: 限流与服务端错误。"""
    return status_code == 429 or status_code >= 500


def stream_error_event(message: str) -> bytes:
    """OpenAI 风格的 SSE 错误事件。"""
    payload = {"error": {"message": message, "type": "upstream_stream_interrupted"}}
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"

Sleeper = Callable[[float], Awaitable[None]]


def create_app(settings: Settings | None = None,
               token_manager: TokenManager | None = None,
               http_client: httpx.AsyncClient | None = None,
               sleep: Sleeper | None = None,
               ledger: Ledger | None = None) -> FastAPI:
    """构造网关应用。参数可注入, 便于测试(sleep 用于跳过真实退避等待)。"""
    settings = settings or load_settings()
    token_manager = token_manager or TokenManager(settings)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
    policy = policy_from_settings(settings)
    sleeper: Sleeper = sleep or anyio.sleep
    ledger = ledger or ledger_from_settings(settings)
    limiter = asyncio.Semaphore(settings.max_concurrency)

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
    app.state.ledger = ledger

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
        """校验本地访问密钥(未设置 KLLM_LOCAL_KEY 时不校验)。

        用 compare_digest 而不是 ==, 避免逐字节比较的时间差泄露密钥前缀。
        """
        if settings.local_key is None:
            return
        expected = f"Bearer {settings.local_key}"
        if not hmac.compare_digest(authorization or "", expected):
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

    @app.get("/v1/usage")
    async def usage(authorization: str | None = Header(default=None)) -> dict:
        """本地估算的今日/本月用量与额度, 附今日/本月按模型分的花费。"""
        check_local_key(authorization)
        return {
            **ledger.status().to_dict(),
            "timezone": settings.usage_timezone,
            "by_model_today": ledger.model_breakdown("today"),
            "by_model_month": ledger.model_breakdown("month"),
        }

    def budget_headers(status) -> dict[str, str]:
        """额度相关响应头(只用 ASCII)。"""
        headers = {
            BUDGET_HEADER: (
                f"today={status.today.cost_dollars:.4f}/{status.daily_limit:.2f};"
                f"month={status.month.cost_dollars:.4f}/{status.monthly_limit:.2f}"
            )
        }
        if status.warning:
            headers[BUDGET_WARNING_HEADER] = (
                f"daily={status.daily_ratio:.0%};monthly={status.monthly_ratio:.0%}"
            )
        return headers

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request,
                               authorization: str | None = Header(default=None)):
        """转发聊天补全请求, 支持流式与非流式。"""
        check_local_key(authorization)
        raw_body = await request.body()
        if len(raw_body) > settings.max_request_bytes:
            raise HTTPException(status_code=413, detail="请求体超出上限")
        try:
            payload = json.loads(raw_body)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

        model = payload.get("model", "")
        streaming = bool(payload.get("stream"))
        logger.info("chat model=%s stream=%s", model, streaming)

        # 先占并发槽位再查额度: 原先先查额度后排队, 排队期间所有请求
        # 都能通过旧账本的检查, 超支上限会被排队长度放大。现在超支最多
        # 是同时在途的 max_concurrency 个请求, 排队请求在真正发往上游
        # 前会再查一次最新账本。
        try:
            if settings.queue_timeout_seconds:
                await asyncio.wait_for(limiter.acquire(),
                                       timeout=settings.queue_timeout_seconds)
            else:
                await limiter.acquire()
        except TimeoutError as error:
            raise HTTPException(status_code=503, detail="网关并发排队超时, 请稍后重试") from error
        released = False

        budget = ledger.status()
        if budget.exceeded:
            limiter.release()
            released = True
            logger.warning("拒绝请求: %s", budget.exceeded)
            return JSONResponse(
                status_code=429,
                content={"error": {"message": f"kllm 本地额度上限: {budget.exceeded}",
                                   "type": "budget_exceeded"}},
                headers=budget_headers(budget),
            )
        if budget.warning:
            logger.warning("额度告警: %s", budget.warning)

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                limiter.release()

        try:
            upstream, used_model = await _send_with_fallback(payload, streaming)
            if streaming:
                response = _streaming_response(upstream, used_model, release)
            else:
                response = await _json_response(upstream, used_model)
                release()
        except BaseException:
            release()
            raise
        response.headers.update(budget_headers(budget))
        response.headers[MODEL_HEADER] = used_model
        return response

    async def _send_with_fallback(payload: dict, streaming: bool) -> tuple[httpx.Response, str]:
        """按 [请求的模型] + 备用模型依次尝试, 返回(响应, 实际使用的模型)。

        全链路总尝试数受 KLLM_MAX_TOTAL_ATTEMPTS 约束, 避免“每模型都重试满”
        把一次请求放大成十几次上游调用。
        """
        requested = payload.get("model", "")
        chain = [requested] + [m for m in settings.fallback_models if m != requested]
        attempt_budget = {"remaining": settings.max_total_attempts or 1_000_000_000}
        for index, model in enumerate(chain):
            is_last = index == len(chain) - 1
            attempt_payload = payload if model == requested else {**payload, "model": model}
            try:
                response = await _send_with_retry(attempt_payload, streaming, attempt_budget)
            except HTTPException as error:
                if is_last or error.status_code != 502:
                    raise
                logger.warning("模型 %s 网络失败, 改用备用模型 %s", model, chain[index + 1])
                continue
            if should_fall_back(response.status_code) and not is_last:
                await response.aclose()
                logger.warning("模型 %s 返回 HTTP %s, 改用备用模型 %s",
                               model, response.status_code, chain[index + 1])
                continue
            return response, model
        raise AssertionError("unreachable")

    async def _send_with_retry(payload: dict, streaming: bool,
                               attempt_budget: dict | None = None) -> httpx.Response:
        """发请求并按策略重试; 网络错误耗尽重试后返回 502。"""
        active = await credentials()
        token_refreshed = False
        attempt = 0
        while True:
            if attempt_budget is not None:
                if attempt_budget["remaining"] <= 0:
                    raise HTTPException(
                        status_code=502,
                        detail="已达总尝试上限(KLLM_MAX_TOTAL_ATTEMPTS), 停止换模型重试",
                    )
                attempt_budget["remaining"] -= 1
            try:
                response = await _send_once(payload, streaming, active)
            except httpx.TransportError as error:
                if attempt >= policy.max_retries or not policy.should_retry_error(error):
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

            if policy.should_retry_status(response.status_code) and attempt < policy.max_retries:
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
            ledger.record(extract_record(model, parsed, streamed=False))
        elif upstream.status_code != 200:
            logger.warning("上游返回 HTTP %s: %s", upstream.status_code, body[:300])
        return JSONResponse(content=parsed, status_code=upstream.status_code)

    def _streaming_response(upstream: httpx.Response, model: str,
                            release: Callable[[], None]) -> StreamingResponse:
        """流式: 字节级透传 SSE, 不解析重组, 保证 tool_calls 分片不失真。

        转发的字节一个不改; 只旁路复制一份用来抽取用量 —— 上游每个 chunk 都带
        累计 usage, 取最后一个见到的即可。上游中途断开时补发 error 事件和 [DONE],
        让客户端明确知道回答不完整。
        """

        async def iterate():
            tail = b""
            last_usage: dict = {}
            incomplete = False
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                    if upstream.status_code == 200:
                        tail, last_usage = _scan_usage(tail + chunk, last_usage)
            except httpx.TransportError as error:
                # 响应头已经发出, 只能在流里告诉客户端出错了。
                incomplete = True
                logger.error("流式转发中断: %s: %s", type(error).__name__, error)
                yield stream_error_event(f"上游流式响应中断({type(error).__name__}), 回答不完整")
                yield STREAM_DONE
            finally:
                await upstream.aclose()
                release()
                if upstream.status_code == 200:
                    ledger.record(extract_record(
                        model, {"usage": last_usage}, streamed=True, incomplete=incomplete,
                    ))

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
