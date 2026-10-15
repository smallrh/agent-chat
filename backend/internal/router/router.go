package router

import (
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/client"
	"agent/auth-service/internal/config"
	"agent/auth-service/internal/db"
	"agent/auth-service/internal/handler"
	"agent/auth-service/internal/middleware"
	"agent/auth-service/internal/model"
	"agent/auth-service/internal/repository"
	"agent/auth-service/internal/service"
)

// Router 封装 Gin Engine
type Router struct {
	engine *gin.Engine
}

// Option router 的可选装配项。
//
// 为什么用 functional options 而不是直接给 New() 加参数：
// New() 的签名被 backend/tests/integration_test.go 直接调用，
// 每加一个必填参数就要改一次测试。改成可选参数后，
// 不注入 database 时计费相关的路由整体不挂载，测试零改动。
type Option func(*routerOptions)

type routerOptions struct {
	database *db.Database
}

// WithDatabase 注入 Postgres + Redis 连接（启用 P3 计费/配额/管理后台）
func WithDatabase(database *db.Database) Option {
	return func(o *routerOptions) { o.database = database }
}

// New 初始化所有路由
func New(
	authHandler *handler.AuthHandler,
	healthHandler *handler.HealthHandler,
	tokenSvc *service.TokenService,
	agentClient *client.AgentClient,
	rateLimitSvc *service.RateLimitService,
	conversationHandler *handler.ConversationHandler,
	opts ...Option,
) *Router {
	o := &routerOptions{}
	for _, opt := range opts {
		opt(o)
	}

	engine := gin.New()

	// ---- P3 计费：只有注入了 database 才启用 ----
	var quotaSvc *service.QuotaService
	var userRepo *repository.AuthUserRepository
	var planRepo *repository.UserPlanRepository
	var cfg *config.Config
	if o.database != nil {
		userRepo = repository.NewAuthUserRepository(o.database.SQL)
		planRepo = repository.NewUserPlanRepository(o.database.SQL, o.database.Cfg)
		quotaSvc = service.NewQuotaService(
			o.database.Redis, planRepo, o.database.Cfg.QuotaEnabled,
		)
		cfg = o.database.Cfg
	}

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

		conv := v1.Group("/conversations")
		conv.Use(middleware.AuthMiddleware(tokenSvc))
		{
			conv.GET("", conversationHandler.List)
			conv.GET("/:id/messages", conversationHandler.ListMessages)
			conv.PATCH("/:id", conversationHandler.Rename)
			conv.DELETE("/:id", conversationHandler.Delete)
		}

		// 记忆 BFF（与会话 API 同一前缀 /api/v1 + 统一信封，不再走 /agent/memories）
		// 在 router 内部构造：复用已有 agentClient，避免改动 New() 签名破坏集成测试。
		memoryHandler := handler.NewMemoryHandler(agentClient)
		mem := v1.Group("/memories")
		mem.Use(middleware.AuthMiddleware(tokenSvc))
		{
			mem.GET("", memoryHandler.List)
			mem.DELETE("/:id", memoryHandler.Delete)
			mem.GET("/:id/source", memoryHandler.Source)
		}

		// 任务 BFF（P2 真任务队列）：/api/v1/tasks → Agent /tasks
		// 除 /tasks/stream 走 SSE 原样透传外，其余都是统一信封 JSON。
		taskHandler := handler.NewTaskHandler(agentClient)
		tasks := v1.Group("/tasks")
		tasks.Use(
			middleware.AuthMiddleware(tokenSvc),
			// 建任务 / 重跑消耗 task 配额；列表、取消、SSE 订阅不消耗
			middleware.QuotaMiddleware(quotaSvc, cfg),
		)
		{
			tasks.GET("", taskHandler.List)
			tasks.POST("", taskHandler.Create)
			// ★ 静态子路径必须声明在 /:id 之前，否则 gin 会把它当成 id
			tasks.GET("/kinds", taskHandler.Kinds)
			tasks.GET("/stream", taskHandler.Stream)
			tasks.GET("/:id", taskHandler.Get)
			tasks.POST("/:id/cancel", taskHandler.Cancel)
			tasks.POST("/:id/retry", taskHandler.Retry)
		}

		// 附件 BFF（P2 多模态）：上传走统一信封，下载走 Raw 二进制透传
		attachmentHandler := handler.NewAttachmentHandler(agentClient)
		atts := v1.Group("/attachments")
		atts.Use(middleware.AuthMiddleware(tokenSvc))
		{
			atts.POST("", attachmentHandler.Upload)
			atts.GET("/:id", attachmentHandler.Download)
		}
		// ---- P3 计费（仅在注入 database 时挂载；不注入则整体不注册）----
		if o.database != nil {
			// 用量自助查询：让用户看得见自己用了多少、还剩多少。
			// 没有它，用户只会在某天突然被 429 拦住却不知道为什么。
			usageHandler := handler.NewUsageHandler(quotaSvc)
			usage := v1.Group("/usage")
			usage.Use(middleware.AuthMiddleware(tokenSvc))
			{
				usage.GET("/me", usageHandler.Me)
			}

			// 管理后台：X-Admin-Token 鉴权；未配置 AdminToken 时整体 404
			adminHandler := handler.NewAdminHandler(userRepo, planRepo, quotaSvc, cfg)
			admin := v1.Group("/admin")
			admin.Use(adminHandler.Guard)
			{
				admin.GET("/users/:uuid/usage", adminHandler.Usage)
				admin.POST("/users/:uuid/status", adminHandler.SetStatus)
				admin.PUT("/users/:uuid/plan", adminHandler.SetPlan)
				admin.GET("/plans", adminHandler.ListPlans)
			}
		}
	}

	// -------------------------------------------------------
	// Agent Proxy（所有 /agent/* → Python Agent /*）
	// 强制认证 + 覆盖 X-User-Id
	// -------------------------------------------------------
	engine.Any("/agent/*path",
		middleware.AuthMiddleware(tokenSvc),
		// 对话护栏：只对 /agent/chat* 生效（频率 + 并发），其余 Agent 接口直接放行
		middleware.ChatGuardMiddleware(rateLimitSvc),
		// P3 配额：只对计费路径生效（chat* / 建任务 / 重跑）；
		// quotaSvc 或 cfg 为 nil 时中间件自动放行。
		middleware.QuotaMiddleware(quotaSvc, cfg),
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
			// 流式请求走 SSE client（整体超时 = AGENT_SSE_TIMEOUT），
			// 否则长回答会在 AGENT_TIMEOUT 处被硬切。
			isStreamingRequest(c),
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

		// SSE 流式响应（/chat/stream 等）必须每次写入后强制 Flush，
		// 否则 gin 的写缓冲（默认 4096B）会把后续事件攒批到缓冲满才发出，
		// 客户端表现为「一次性吐出 / 卡顿」。普通 JSON 响应无需逐块 Flush，
		// 直接 io.Copy 即可（减少系统调用开销）。
		if isStreamingResponse(resp) {
			_, _ = io.Copy(&flushWriter{w: c.Writer}, resp.Body)
		} else {
			_, _ = io.Copy(c.Writer, resp.Body)
		}
	}
}

// flushWriter 包装 http.ResponseWriter，每次 Write 后强制 Flush。
// 仅用于 SSE 流式代理透传：保证 Agent 产生的每个事件实时推送给客户端，
// 避免被 gin 的写缓冲攒批。普通响应不要用，以免不必要的 Flush 开销。
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

// isStreamingResponse 判断 Agent 响应是否为 SSE 流式（text/event-stream）。
func isStreamingResponse(resp *http.Response) bool {
	return strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream")
}

// isStreamingRequest 判断**客户端发起的**请求是否为 SSE 流式请求。
//
// 必须在发出代理请求「之前」判断（那时还没有 Agent 响应），依据两条：
//  1. 路径命中 /chat/stream（本项目唯一的长流接口）
//  2. 客户端显式声明 Accept: text/event-stream（兜底，未来新增流式接口也覆盖）
func isStreamingRequest(c *gin.Context) bool {
	p := strings.TrimPrefix(c.Param("path"), "/")
	if p == "chat/stream" {
		return true
	}
	return strings.Contains(c.GetHeader("Accept"), "text/event-stream")
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
