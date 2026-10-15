# Agent Proxy 规范

## 0. 概述

Go Auth Service 是所有客户端请求的入口，Python Agent 不直接暴露给客户端。

当前路由映射（见 [router.go](file:///d:/work/agent_system/backend/internal/router/router.go)）：

```
客户端请求路径                  →  Go Auth 处理        →  Python Agent 路径
────────────────────────────────────────────────────────────────────────
POST /api/v1/auth/register    →  Go Auth 自己处理     →  不转发
POST /api/v1/auth/login       →  Go Auth 自己处理     →  不转发
POST /api/v1/auth/refresh     →  Go Auth 自己处理     →  不转发
POST /api/v1/auth/logout      →  Go Auth 自己处理     →  不转发
GET  /api/v1/auth/me          →  Go Auth 自己处理     →  不转发

ANY  /agent/*path             →  AuthMiddleware + 代理  →  /{path}
                                                         例: /agent/chat → /chat
                                                             /agent/chat/stream → /chat/stream
```

**新增代理路由**时必须：

1. 先过 `AuthMiddleware`（验证 JWT）
2. 调用 `agentClient.ProxyToAgent()`
3. 正确设置 `X-User-Id`（见 [12-跨服务契约规范.md](file:///d:/work/agent_system/backend/.agent/rules/12-跨服务契约规范.md) 第 1 节）

---

## 1. Agent Client

统一入口：[agent_client.go](file:///d:/work/agent_system/backend/internal/client/agent_client.go)（`internal/client/agent_client.go`）

所有 Go → Python 请求必须经过这个 Client。**禁止 Handler 直接 `http.Get/Post`**。

### Client 构造

`NewAgentClient(cfg)` 自动配置可复用 Transport：

- `MaxIdleConnsPerHost`
- `ResponseHeaderTimeout = cfg.AgentTimeout`
- `IdleConnTimeout`

---

## 2. 身份传递（安全层）

### Go Auth 必须做的事

1. `AuthMiddleware` 验证 JWT → 取出 `sub`（user_uuid）
2. `agentProxyHandler` 从 `AuthContext` 拿到 `authCtx.UserUUID`
3. `AgentClient.ProxyToAgent()` 在转发前：
   - 删除客户端自带的 `X-User-Id` / `X-User-UUID` / `X-Client-User-Id`
   - 强制注入 `X-User-Id = userUUID`（UUID string）

### 不要重复

安全原则详见：

- [04-认证安全规范.md](file:///d:/work/agent_system/backend/.agent/rules/04-认证安全规范.md) 第 11 节（X-User-Id 安全边界）
- [AGENTS.md](file:///d:/work/agent_system/backend/AGENTS.md) "不可违反的核心安全边界"

本文件**不再重复**上述安全原则。

---

## 3. Timeout

必须配置 `AGENT_TIMEOUT`（见 [config.go](file:///d:/work/agent_system/backend/internal/config/config.go)）。

默认 30s。**禁止无限等待 Agent**。

超时处理（见 `isTimeoutError()` + `AgentError.Status=504`）：

- `context deadline exceeded` → 504 Gateway Timeout
- `i/o timeout` → 504 Gateway Timeout
- `Client.Timeout exceeded` → 504 Gateway Timeout
- 其他连接错误（connection refused）→ 502 Bad Gateway

---

## 4. Request ID 传递

Go → Agent：

```
X-Request-Id  ←  来自 AuthMiddleware/RequestIDMiddleware
                ↓
                middleware.GetRequestIDFromContext(ctx)
                ↓
                agentClient 注入到 Agent 请求 Header
```

如果 context 里没有 Request ID，fallback 用 `fmt.Sprintf("gen-%d", time.Now().UnixNano())`。

---

## 5. Header 过滤

### Go Auth → Agent 请求时

**删除**：

| Header | 原因 |
|--------|------|
| `X-User-Id` 系列 | 客户端不可信，Go 强制覆盖 |
| `Authorization` | Python Agent 不需要客户端 JWT |
| `Cookie` | Python Agent 不读 Cookie |

**保留**：

| Header | 原因 |
|--------|------|
| `Content-Type` | Agent 需要知道 Body 格式 |
| `Accept` | Agent 响应格式协商 |
| `User-Agent` | Agent 日志可参考 |
| `X-Client-IP` | Agent 审计用 |

**新增**：

| Header | 值 |
|--------|-----|
| `X-User-Id` | user_uuid（Go Auth 验证后的） |
| `X-Request-Id` | 全链路 Request ID |

### Agent → Go Auth 响应透传时

**跳过 hop-by-hop headers**（HTTP/1.1 规范）：

```go
switch k {
case "Transfer-Encoding", "Connection", "Keep-Alive", "Proxy-Authenticate":
    continue
}
```

其余 Header 原样透传给客户端。

---

## 6. 响应体透传

### 普通 JSON 响应

当前 router 实现：

```go
_, _ = io.Copy(c.Writer, resp.Body)
```

Go Auth 不解析 Agent 返回的 JSON，原样透传。好处：

- 避免额外序列化开销
- Go Auth 不需要知道 Agent 的响应结构
- Agent 可以独立演进响应格式

### SSE 流式响应

`io.Copy` 本身能透传流式数据（`c.Writer` 的 `io.Writer` + `resp.Body` 的 `io.Reader`），但 **Gin 的 `c.Writer` 默认带 4096 字节写缓冲**：`io.Copy` 不会在每次 `Write` 后主动 `Flush`，SSE 事件要攒到缓冲满（或响应结束）才推给客户端，表现为"一卡一卡 / 一次性吐出"。

因此当前实现用 `flushWriter` 包装 `c.Writer`，**每个 SSE 事件写入后立即 `http.Flusher.Flush()`**（不支持 Flush 时安全降级为普通写入）。是否为 SSE 由 `isStreamingResponse()` 按响应 `Content-Type` 是否含 `text/event-stream` 判断：命中则走 `flushWriter` 逐块强制刷出；普通 JSON 响应仍直连 `io.Copy`，避免无谓的 Flush 开销。

但需要注意：

1. **必须提前写 Response Header**：`c.Status(resp.StatusCode)` + `c.Writer.WriteHeaderNow()` —— 在透传之前，避免 Gin 缓冲 Header
2. **必须显式 Flush**：通过 `flushWriter`（包裹 `c.Writer`，每次 `Write` 后调用 `http.Flusher.Flush()`）逐块刷出 SSE 事件，保证客户端实时收到 token
3. **不能用 `c.JSON()` / `c.String()`** —— 否则响应会被 Gin 缓冲
4. **不能调用 `c.Next()`** —— SSE 要持续写，不能等中间件链完成
5. **SSE 超时**：`AGENT_TIMEOUT` 配置的是 HTTP Client 总超时，SSE 长连接可能调大（当前实现 `AgentClient.Timeout` 覆盖所有请求，SSE 可能需要单独配置 `AGENT_STREAM_TIMEOUT`）

### Body 读取注意事项

Gin 的 `c.Request.Body` 是 `io.ReadCloser`，**读一次就没了**。当前 router 在调用 `agentClient.ProxyToAgent` 之前读取 Body 并 close，然后 agentClient 用 `bytes.NewReader` 重新构造 Body Reader。

**禁止**：

- 在 Handler 内用 `c.BindJSON()` 后又要转发给 Agent —— Body 会被消费两次
- 在 `ProxyToAgent` 里尝试再次读取 Body —— 已经在 router 层读完了

---

## 7. 错误处理

### AgentError 结构

```go
type AgentError struct {
    Code    string
    Message string
    Status  int
    Err     error
}
```

**Handler 层只看 Status**：

| Status | 场景 | 客户端收到 |
|--------|------|-----------|
| 504 | Agent 超时 | `{"code":"GATEWAY_TIMEOUT", "message":"Agent service timed out", "request_id":"..."}` |
| 502 | Agent 不可用 / 返回异常 | `{"code":"BAD_GATEWAY", "message":"Agent service is unavailable", "request_id":"..."}` |

### 禁止泄露内部信息

**禁止**把以下信息返回给客户端：

- Python Exception
- Stack Trace
- 内部 URL（如 `http://agent-service:1111/chat`）
- 数据库错误
- Kafka 错误

详细错误只打服务端日志（`slog.Error(...)`）。

---

## 8. 职责边界

### Go Auth 不应该做的事

- 解析 Agent 的 ChatRequest JSON Body（**禁止在代理层修改 Body 内容**）
- 做 Agent 的 Conversation / Memory 业务逻辑
- 调 LLM

### Python Agent 不应该做的事

- 验 JWT（Go Auth 已经验过了）
- 存 auth_users 表数据

### 当前 Body 里 user_id 字段的处理

Go Auth 当前**不会**修改转发的 ChatRequest Body。Python Agent 侧如果 Body 里有 `user_id` 字段（int 类型），而 Go Auth Header 里传的是 `X-User-Id`（UUID string），会出现类型冲突。

两种方案：

**方案 A（推荐）**：Python Agent 侧去掉 ChatRequest.user_id 字段，完全从 Header 读（见 [12-跨服务契约规范.md](file:///d:/work/agent_system/backend/.agent/rules/12-跨服务契约规范.md) 第 2 节修复步骤）

**方案 B（临时 workaround）**：Go Auth 代理层在转发前，把 Body 里的 `user_id`（如果存在）替换成 Python 侧实际需要的 `users.id`（int）。但 Go Auth 没有 `users.id`，所以不可行。

**结论**：必须走方案 A。Go Auth 代理层只负责 Header 注入，Body 不改。
