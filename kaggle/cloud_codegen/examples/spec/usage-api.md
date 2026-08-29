# Usage API Contract

服务默认地址为 `http://127.0.0.1:8787`，请求和响应均使用 JSON。

## GET `/api/usage`

可选查询参数 `providers`，值为逗号分隔的 provider 名称。成功时返回任意合法 JSON 对象。
未知 provider 返回 HTTP 400，响应为：

```json
{"error": "unknown provider: example"}
```

## GET `/api/providers`

成功时返回 provider 配置：

```json
{"providers": {"example": true}}
```

客户端只返回 `providers` 字段中的映射。

## PUT `/api/providers`

请求体：

```json
{"providers": {"example": false}}
```

成功响应与 GET `/api/providers` 相同，客户端只返回内部映射。

## 错误处理

- HTTP 400 抛出 `UnknownProviderError`。
- 其他非 2xx 响应抛出 `UsageError`。
- 异常消息必须包含响应 `error` 字段的文本。
