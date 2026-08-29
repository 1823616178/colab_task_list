"""Phase 0 可行性探测: 验证 Kaggle 模型代理是否可用(含地区限制门禁)。

用法: python kaggle/tests/probe_proxy.py
不写任何持久文件, 只打印结论。
"""

import json
import sys
import time

import httpx

PROBE_MODEL = "google/gemini-3-flash-preview"
CLAUDE_MODEL = "anthropic/claude-sonnet-5@default"
# OpenAI 兼容端点在 MODEL_PROXY_URL 之后还有 /openapi (kaggle_benchmarks/kaggle/model_proxy.py:94)
OPENAI_SUFFIX = "/openapi"


def fetch_credentials() -> dict[str, str]:
    """调用 Kaggle CLI 私有入口获取模型代理凭据, 不落盘。"""
    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    api.authenticate()
    return api._fetch_model_proxy_env("auth")


def chat(base_url: str, token: str, model: str, prompt: str, stream: bool = False,
         tools: list | None = None) -> httpx.Response:
    """向代理发一次 chat/completions 请求。"""
    payload: dict = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = tools
    return httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=120.0,
    )


def main() -> int:
    print("[1] 获取模型代理凭据 ...")
    try:
        env = fetch_credentials()
    except Exception as error:  # noqa: BLE001
        print(f"    失败: {type(error).__name__}: {error}")
        return 1
    base_url = env["MODEL_PROXY_URL"].rstrip("/") + OPENAI_SUFFIX
    token = env["MODEL_PROXY_API_KEY"]
    print(f"    base_url = {base_url}")
    print(f"    token    = ...{token[-4:]}  expiry = {env.get('MODEL_PROXY_EXPIRY_TIME')}")

    print(f"\n[2] 基础调用 ({PROBE_MODEL}) ...")
    response = chat(base_url, token, PROBE_MODEL, "Reply with exactly: OK")
    print(f"    HTTP {response.status_code}")
    body = response.text
    if response.status_code != 200:
        print(f"    响应: {body[:600]}")
        if "not supported" in body and "location" in body:
            print("\n>>> 门禁失败: 地区限制仍然存在, 方案不成立。")
        return 1
    data = response.json()
    print(f"    内容: {data['choices'][0]['message']['content'][:120]!r}")
    print(f"    用量: {json.dumps(data.get('usage', {}))}")

    print(f"\n[3] Claude 模型 ({CLAUDE_MODEL}) ...")
    response = chat(base_url, token, CLAUDE_MODEL, "Reply with exactly: OK")
    print(f"    HTTP {response.status_code}")
    if response.status_code == 200:
        print(f"    内容: {response.json()['choices'][0]['message']['content'][:120]!r}")
    else:
        print(f"    响应: {response.text[:400]}")

    print("\n[4] 流式 ...")
    started = time.time()
    with httpx.stream(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "model": PROBE_MODEL,
            "messages": [{"role": "user", "content": "Count 1 to 5, one per line."}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
        timeout=120.0,
    ) as stream_response:
        print(f"    HTTP {stream_response.status_code}")
        chunk_count = 0
        last_line = ""
        for line in stream_response.iter_lines():
            if line.strip():
                chunk_count += 1
                last_line = line
        print(f"    收到 {chunk_count} 行, 耗时 {time.time() - started:.1f}s")
        print(f"    末行: {last_line[:200]}")

    print("\n[5] tool calling ...")
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }]
    response = chat(base_url, token, PROBE_MODEL, "What is the weather in Berlin?", tools=tools)
    print(f"    HTTP {response.status_code}")
    if response.status_code == 200:
        message = response.json()["choices"][0]["message"]
        print(f"    tool_calls: {json.dumps(message.get('tool_calls'))[:300]}")
    else:
        print(f"    响应: {response.text[:400]}")

    print("\n[6] GET /models ...")
    models_response = httpx.get(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    print(f"    HTTP {models_response.status_code}: {models_response.text[:400]}")

    print("\n>>> 门禁通过: 代理可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
