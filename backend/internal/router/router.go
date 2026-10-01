package router

import (
	"errors"
	"io"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/client"
	"agent/auth-service/internal/handler"
	"agent/auth-service/internal/middleware"
	"agent/auth-service/internal/model"
	"agent/auth-service/internal/service"
)

// Router 封装 Gin Engine
type Router struct {
	engine *gin.Engine
}

// New 初始化所有路由
func New(
	authHandler *handler.AuthHandler,
	healthHandler *handler.HealthHandler,
	tokenSvc *service.TokenService,
	agentClient *client.AgentClient,
	rateLimitSvc *service.RateLimitService,
) *Router {
	engine := gin.New()

	// 全局中间件
	engine.Use(gin.Recovery())
	engine.Use(middleware.RequestIDMiddleware())
	engine.Use(middleware.LoggingMiddleware())

	// -------------------------------------------------------
	// 健康检查
	// -------------------------------------------------------
	engine.GET("/health", healthHandler.Health)
	engine.GET("/ready", healthHandler.Ready)

	// -------------------------------------------------------
	// Auth API（不需要认证）
	// -------------------------------------------------------
	v1 := engine.Group("/api/v1")
	{
		auth := v1.Group("/auth")
		{
			// 注册 + 登录（带限流）
			auth.POST("/register",
				middleware.RateLimitMiddleware(rateLimitSvc, "register"),
				authHandler.Register,
			)
			auth.POST("/login",
				middleware.RateLimitMiddleware(rateLimitSvc, "login"),
				authHandler.Login,
			)
			auth.POST("/refresh", authHandler.Refresh)
		}

		// 需要认证的 Auth API
		authProtected := v1.Group("/auth")
		authProtected.Use(middleware.AuthMiddleware(tokenSvc))
		{
			authProtected.POST("/logout", authHandler.Logout)
			authProtected.GET("/me", authHandler.Me)
		}
	}

	// -------------------------------------------------------
	// Agent Proxy（所有 /agent/* → Python Agent /*）
	// 强制认证 + 覆盖 X-User-Id
	// -------------------------------------------------------
	engine.Any("/agent/*path",
		middleware.AuthMiddleware(tokenSvc),
		agentProxyHandler(agentClient),
	)

	return &Router{engine: engine}
}

// Run 启动 HTTP Server
func (r *Router) Run(addr string) error {
	return r.engine.Run(addr)
}

// Handler 返回 http.Handler（供测试用）
func (r *Router) Handler() http.Handler {
	return r.engine
}

// ------------------------------------------------------------
// Agent Proxy Handler
// ------------------------------------------------------------

// agentProxyHandler 把 /agent/*path 转发到 Python Agent
// path: /agent/chat → 转发到 http://agent:1111/chat
func agentProxyHandler(agentClient *client.AgentClient) gin.HandlerFunc {
	return func(c *gin.Context) {
		// 从 Auth Context 获取真实身份（JWT sub）
		authCtx := middleware.GetAuthContext(c)
		if authCtx == nil || authCtx.UserUUID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, model.ErrorResponse{
				Code:    "UNAUTHORIZED",
				Message: "Authentication required",
			})
			return
		}

		// 提取 path：/agent/chat → /chat
		targetPath := c.Param("path")
		if targetPath == "" {
			targetPath = "/"
		}

		// 读取 Body
		var bodyBytes []byte
		if c.Request.Body != nil {
			b, err := io.ReadAll(c.Request.Body)
			if err != nil {
				writeProxyError(c, http.StatusBadRequest, "Failed to read request body", nil)
				return
			}
			bodyBytes = b
			// 恢复 Body 以便后续可能使用
			_ = c.Request.Body.Close()
		}

		// 构造 Header（除了必须覆盖的 X-User-Id 外，保留 Content-Type / Accept 等）
		forwardHeaders := http.Header{}
		for k, v := range c.Request.Header {
			forwardHeaders[k] = v
		}
		forwardHeaders.Set("X-Client-IP", c.ClientIP())

		// 转发
		ctx := middleware.WithRequestID(c.Request.Context(), middleware.GetRequestID(c))

		resp, err := agentClient.ProxyToAgent(
			ctx,
			c.Request.Method,
			targetPath,
			authCtx.UserUUID, // 从 JWT 来的真实身份
			bodyBytes,
			forwardHeaders,
		)

		if err != nil {
			var ae *client.AgentError
			if errors.As(err, &ae) {
				writeProxyError(c, ae.Status, ae.Message, err)
			} else {
				writeProxyError(c, http.StatusBadGateway, "Agent service unavailable", err)
			}
			return
		}
		defer resp.Body.Close()

		// 把 Agent 的响应原样透传给客户端
		copyResponseHeaders(c, resp)
		c.Status(resp.StatusCode)
		c.Writer.WriteHeaderNow()
		_, _ = io.Copy(c.Writer, resp.Body)
	}
}

// copyResponseHeaders 把 Agent 的响应 Header 透传给客户端
func copyResponseHeaders(c *gin.Context, resp *http.Response) {
	for k, v := range resp.Header {
		// 跳过 hop-by-hop headers
		switch k {
		case "Transfer-Encoding", "Connection", "Keep-Alive", "Proxy-Authenticate":
			continue
		}
		for _, val := range v {
			c.Writer.Header().Add(k, val)
		}
	}
}

// writeProxyError Agent Proxy 错误响应
func writeProxyError(c *gin.Context, status int, msg string, err error) {
	requestID := middleware.GetRequestID(c)
	if err != nil {
		slog.Error("Agent proxy error", "status", status, "msg", msg, "err", err, "request_id", requestID)
	}
	c.AbortWithStatusJSON(status, model.ErrorResponse{
		Code:      mapStatusCode(status),
		Message:   msg,
		RequestID: requestID,
	})
}

func mapStatusCode(status int) string {
	switch status {
	case http.StatusGatewayTimeout:
		return "GATEWAY_TIMEOUT"
	case http.StatusBadGateway:
		return "BAD_GATEWAY"
	case http.StatusUnauthorized:
		return "UNAUTHORIZED"
	default:
		return "INTERNAL_ERROR"
	}
}
