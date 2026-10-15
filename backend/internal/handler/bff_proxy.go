package handler

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/client"
	"agent/auth-service/internal/middleware"
	"agent/auth-service/internal/model"
)

// BFFProxy 把「鉴权后的 BFF 请求」转发到 Python Agent，并统一包装成信封 {code,message,data}。
//
// 抽出来的原因：会话 BFF 与记忆 BFF（以及未来的任务 BFF）走的是**完全相同**的转发逻辑——
// 鉴权 → 读 body → 转发 → 错误码映射 → 包信封。原先这段代码只存在于 ConversationHandler 里，
// 新增 BFF 只能复制一份，改一处要改两处。现在两边共用。
type BFFProxy struct {
	agent *client.AgentClient
}

// NewBFFProxy 构造 BFF 转发器
func NewBFFProxy(agent *client.AgentClient) *BFFProxy {
	return &BFFProxy{agent: agent}
}

// JSON 转发一个普通 JSON 请求到 Agent（非流式）。
// agentPath 如 "/memories"；rawQuery 为原始查询串（可为空）。
func (p *BFFProxy) JSON(c *gin.Context, agentPath, rawQuery string) {
	authCtx := middleware.GetAuthContext(c)
	if authCtx == nil || authCtx.UserUUID == "" {
		c.AbortWithStatusJSON(http.StatusUnauthorized, model.ErrorResponse{
			Code:    "UNAUTHORIZED",
			Message: "Authentication required",
		})
		return
	}

	var bodyBytes []byte
	if c.Request.Body != nil {
		b, err := io.ReadAll(c.Request.Body)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusBadRequest, model.ErrorResponse{
				Code:    "INVALID_REQUEST",
				Message: "Failed to read request body",
			})
			return
		}
		bodyBytes = b
	}

	path := agentPath
	if rawQuery != "" {
		path = agentPath + "?" + rawQuery
	}

	forwardHeaders := http.Header{}
	if ct := c.GetHeader("Content-Type"); ct != "" {
		forwardHeaders.Set("Content-Type", ct)
	}
	forwardHeaders.Set("Accept", "application/json")
	forwardHeaders.Set("X-Client-IP", c.ClientIP())

	ctx := middleware.WithRequestID(c.Request.Context(), middleware.GetRequestID(c))
	resp, err := p.agent.ProxyToAgent(
		ctx,
		c.Request.Method,
		path,
		authCtx.UserUUID,
		bodyBytes,
		forwardHeaders,
		false, // BFF 均为普通 JSON 请求
	)
	if err != nil {
		var ae *client.AgentError
		if errors.As(err, &ae) {
			c.AbortWithStatusJSON(ae.Status, model.ErrorResponse{
				Code:      ae.Code,
				Message:   ae.Message,
				RequestID: middleware.GetRequestID(c),
			})
			return
		}
		c.AbortWithStatusJSON(http.StatusBadGateway, model.ErrorResponse{
			Code:      "BAD_GATEWAY",
			Message:   "Agent service unavailable",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}
	defer resp.Body.Close()

	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		msg := http.StatusText(resp.StatusCode)
		var parsed map[string]any
		if json.Unmarshal(raw, &parsed) == nil {
			if d, ok := parsed["detail"].(string); ok && d != "" {
				msg = d
			}
		}
		code := "AGENT_ERROR"
		if resp.StatusCode == http.StatusForbidden {
			code = "FORBIDDEN"
		} else if resp.StatusCode == http.StatusNotFound {
			code = "NOT_FOUND"
		}
		c.AbortWithStatusJSON(resp.StatusCode, model.ErrorResponse{
			Code:      code,
			Message:   msg,
			RequestID: middleware.GetRequestID(c),
		})
		return
	}

	var data any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &data)
	}
	writeOK(c, http.StatusOK, data)
}

// flushWriter 每次 Write 后强制 Flush，用于 SSE 透传。
// （router 包里有一个同名的，但那是给 /agent/* 代理用的；Go 里不同包
//  需要各自定义，这里复制一份最小实现。）
type flushWriter struct {
	w http.ResponseWriter
}

func (f *flushWriter) Write(p []byte) (int, error) {
	n, err := f.w.Write(p)
	if n > 0 {
		if flusher, ok := f.w.(http.Flusher); ok {
			flusher.Flush()
		}
	}
	return n, err
}

// ---------------------------------------------------------------------------
// 流式 / 二进制透传
// ---------------------------------------------------------------------------

// Stream 转发 SSE 流式请求到 Agent，并把响应**原样**透传给客户端。
//
// 为什么不走 JSON 包信封：SSE 是「一条长连接上持续吐事件」的协议，
// 任何中间缓冲都会把实时性毁掉（表现为事件攒批、一次性吐出）。
// 因此这里：
//  1. 用 sse 专用的 http.Client（整体超时 = AGENT_SSE_TIMEOUT），
//     否则任务流会在 AGENT_TIMEOUT（默认 30s）处被硬切；
//  2. 每次 Write 后强制 Flush，绕开 gin 的 4KB 写缓冲；
//  3. 透传 Content-Type / Cache-Control / X-Accel-Buffering。
func (p *BFFProxy) Stream(c *gin.Context, agentPath, rawQuery string) {
	authCtx := middleware.GetAuthContext(c)
	if authCtx == nil || authCtx.UserUUID == "" {
		c.AbortWithStatusJSON(http.StatusUnauthorized, model.ErrorResponse{
			Code:    "UNAUTHORIZED",
			Message: "Authentication required",
		})
		return
	}

	// SSE 订阅是 GET，无 body；但保留读取逻辑以便未来扩展
	var bodyBytes []byte
	if c.Request.Body != nil {
		b, err := io.ReadAll(c.Request.Body)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusBadRequest, model.ErrorResponse{
				Code:    "INVALID_REQUEST",
				Message: "Failed to read request body",
			})
			return
		}
		bodyBytes = b
	}

	path := agentPath
	if rawQuery != "" {
		path = agentPath + "?" + rawQuery
	}

	forwardHeaders := http.Header{}
	forwardHeaders.Set("Accept", "text/event-stream")
	forwardHeaders.Set("X-Client-IP", c.ClientIP())

	ctx := middleware.WithRequestID(c.Request.Context(), middleware.GetRequestID(c))
	resp, err := p.agent.ProxyToAgent(
		ctx,
		c.Request.Method,
		path,
		authCtx.UserUUID,
		bodyBytes,
		forwardHeaders,
		true, // ← 走 SSE client（长超时）
	)
	if err != nil {
		var ae *client.AgentError
		if errors.As(err, &ae) {
			c.AbortWithStatusJSON(ae.Status, model.ErrorResponse{
				Code:      ae.Code,
				Message:   ae.Message,
				RequestID: middleware.GetRequestID(c),
			})
			return
		}
		c.AbortWithStatusJSON(http.StatusBadGateway, model.ErrorResponse{
			Code:      "BAD_GATEWAY",
			Message:   "Agent service unavailable",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		raw, _ := io.ReadAll(resp.Body)
		msg := http.StatusText(resp.StatusCode)
		var parsed map[string]any
		if json.Unmarshal(raw, &parsed) == nil {
			if d, ok := parsed["detail"].(string); ok && d != "" {
				msg = d
			}
		}
		c.AbortWithStatusJSON(resp.StatusCode, model.ErrorResponse{
			Code:      "AGENT_ERROR",
			Message:   msg,
			RequestID: middleware.GetRequestID(c),
		})
		return
	}

	// 透传响应头（跳过 hop-by-hop）
	for k, vals := range resp.Header {
		switch k {
		case "Transfer-Encoding", "Connection", "Keep-Alive", "Proxy-Authenticate":
			continue
		}
		for _, v := range vals {
			c.Writer.Header().Add(k, v)
		}
	}
	// 明确关掉反向代理缓冲，否则事件会被攒批
	if c.Writer.Header().Get("X-Accel-Buffering") == "" {
		c.Writer.Header().Set("X-Accel-Buffering", "no")
	}
	c.Status(resp.StatusCode)
	c.Writer.WriteHeaderNow()

	_, _ = io.Copy(&flushWriter{w: c.Writer}, resp.Body)
}

// Raw 把 Agent 的响应**原样**透传（状态码 + 头 + 二进制 body），不包信封。
//
// 用于附件下载：客户端要拿到的是图片/文件的原始字节流和 Content-Type，
// 套一层 {code,message,data} 反而让 <img src> / <a download> 用不了。
func (p *BFFProxy) Raw(c *gin.Context, agentPath, rawQuery string) {
	authCtx := middleware.GetAuthContext(c)
	if authCtx == nil || authCtx.UserUUID == "" {
		c.AbortWithStatusJSON(http.StatusUnauthorized, model.ErrorResponse{
			Code:    "UNAUTHORIZED",
			Message: "Authentication required",
		})
		return
	}

	var bodyBytes []byte
	if c.Request.Body != nil {
		b, err := io.ReadAll(c.Request.Body)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusBadRequest, model.ErrorResponse{
				Code:    "INVALID_REQUEST",
				Message: "Failed to read request body",
			})
			return
		}
		bodyBytes = b
	}

	path := agentPath
	if rawQuery != "" {
		path = agentPath + "?" + rawQuery
	}

	// 完整透传客户端的 Content-Type / Accept —— 上传是 multipart，
	// 下载要保留图片类型，都不能被写死成 application/json。
	forwardHeaders := http.Header{}
	for _, h := range []string{"Content-Type", "Accept"} {
		if v := c.GetHeader(h); v != "" {
			forwardHeaders.Set(h, v)
		}
	}
	forwardHeaders.Set("X-Client-IP", c.ClientIP())

	ctx := middleware.WithRequestID(c.Request.Context(), middleware.GetRequestID(c))
	resp, err := p.agent.ProxyToAgent(
		ctx, c.Request.Method, path, authCtx.UserUUID, bodyBytes, forwardHeaders, false,
	)
	if err != nil {
		var ae *client.AgentError
		if errors.As(err, &ae) {
			c.AbortWithStatusJSON(ae.Status, model.ErrorResponse{
				Code:      ae.Code,
				Message:   ae.Message,
				RequestID: middleware.GetRequestID(c),
			})
			return
		}
		c.AbortWithStatusJSON(http.StatusBadGateway, model.ErrorResponse{
			Code:      "BAD_GATEWAY",
			Message:   "Agent service unavailable",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}
	defer resp.Body.Close()

	for k, vals := range resp.Header {
		switch k {
		case "Transfer-Encoding", "Connection", "Keep-Alive", "Proxy-Authenticate":
			continue
		}
		for _, v := range vals {
			c.Writer.Header().Add(k, v)
		}
	}
	c.Status(resp.StatusCode)
	c.Writer.WriteHeaderNow()
	if resp.Body != nil {
		_, _ = io.Copy(c.Writer, resp.Body)
	}
}
