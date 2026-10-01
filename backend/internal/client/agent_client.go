package client

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"agent/auth-service/internal/config"
	"agent/auth-service/internal/middleware"
)

// AgentClient Go → Python Agent 代理层
// 核心职责：
// 1. 复用 http.Client 连接池
// 2. 覆盖/强制注入 X-User-Id（从 JWT sub 来，不信任客户端）
// 3. 注入/校验 X-Request-Id
// 4. 合理超时
// 5. 统一错误处理（区分 502 vs 504）
type AgentClient struct {
	cfg  *config.Config
	http *http.Client
}

// NewAgentClient 构造可复用 HTTP Client
func NewAgentClient(cfg *config.Config) *AgentClient {
	transport := &http.Transport{
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 20,
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
		// 关键：不允许客户端无限等待
		ResponseHeaderTimeout: cfg.AgentTimeout,
		ExpectContinueTimeout: 1 * time.Second,
	}

	return &AgentClient{
		cfg: cfg,
		http: &http.Client{
			Timeout:   cfg.AgentTimeout,
			Transport: transport,
		},
	}
}

// ProxyToAgent 把请求代理到 Python Agent
// userUUID: 从 JWT sub 拿到的真实身份（强制覆盖 X-User-Id）
func (c *AgentClient) ProxyToAgent(ctx context.Context, method string, path string, userUUID string, body []byte, headers http.Header) (*http.Response, error) {
	targetURL := strings.TrimRight(c.cfg.AgentServiceURL, "/") + path

	// 构造 Request ID（优先从 context 取）
	requestID := middleware.GetRequestIDFromContext(ctx)
	if requestID == "" {
		requestID = fmt.Sprintf("gen-%d", time.Now().UnixNano())
	}

	slog.Info("Proxy to Agent",
		"method", method,
		"target", targetURL,
		"user_uuid", userUUID,
		"request_id", requestID,
		"client_ip", headers.Get("X-Client-IP"),
	)

	var bodyReader io.Reader
	if len(body) > 0 {
		bodyReader = bytes.NewReader(body)
	}

	req, err := http.NewRequestWithContext(ctx, method, targetURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("build proxy request: %w", err)
	}

	// ---- 关键安全步骤 ----
	// 1. 复制必要 Header，但必须删除客户端自带的 X-User-Id
	// 2. 用 userUUID 重新写入 X-User-Id
	// ----------------------

	// 删除所有可能伪造身份的 Header
	for _, h := range []string{"X-User-Id", "X-User-UUID", "X-Client-User-Id"} {
		req.Header.Del(h)
	}

	// 保留有用的标准 Header（不保留 Authorization —— Python Agent 不需要客户端的 JWT）
	for _, h := range []string{"Content-Type", "Accept", "User-Agent"} {
		if v := headers.Get(h); v != "" {
			req.Header.Set(h, v)
		}
	}

	// 3. 强制注入真实身份（从 JWT sub 来）
	req.Header.Set("X-User-Id", userUUID)

	// 4. 注入 / 保留 Request ID
	req.Header.Set("X-Request-Id", requestID)

	// ---- 发起请求 ----
	resp, err := c.http.Do(req)
	if err != nil {
		// 判断超时 vs 连接拒绝
		if isTimeoutError(err) {
			slog.Error("Agent proxy timeout", "target", targetURL, "request_id", requestID)
			return nil, &AgentError{
				Code:    "GATEWAY_TIMEOUT",
				Message: "Agent service timed out",
				Status:  http.StatusGatewayTimeout,
				Err:     err,
			}
		}

		slog.Error("Agent proxy connection error", "target", targetURL, "request_id", requestID, "err", err)
		return nil, &AgentError{
			Code:    "BAD_GATEWAY",
			Message: "Agent service is unavailable",
			Status:  http.StatusBadGateway,
			Err:     err,
		}
	}

	// 记录 Agent 返回状态
	slog.Info("Agent proxy response",
		"status", resp.StatusCode,
		"request_id", requestID,
	)

	return resp, nil
}

// AgentError Agent 代理层专属错误
type AgentError struct {
	Code    string
	Message string
	Status  int
	Err     error
}

func (e *AgentError) Error() string {
	if e.Err != nil {
		return fmt.Sprintf("[%s] %s: %v", e.Code, e.Message, e.Err)
	}
	return fmt.Sprintf("[%s] %s", e.Code, e.Message)
}

// Unwrap 支持 errors.Is / errors.As
func (e *AgentError) Unwrap() error { return e.Err }

// isTimeoutError 判断是否为超时
func isTimeoutError(err error) bool {
	if err == nil {
		return false
	}
	return strings.Contains(err.Error(), "context deadline exceeded") ||
		strings.Contains(err.Error(), "Client.Timeout exceeded") ||
		strings.Contains(err.Error(), "i/o timeout")
}
