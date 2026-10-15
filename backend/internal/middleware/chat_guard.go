package middleware

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/model"
	"agent/auth-service/internal/service"
)

// ChatGuardMiddleware 对话接口护栏（挂在 /agent/* 上，只对 chat 路径生效）：
//
//  1. 频率限流：CheckChat —— 单用户每分钟多少次对话请求
//  2. 并发限流：AcquireStreamSlot —— 单用户同时最多几条 SSE 流
//
// 非 chat 路径（如 /agent/memories、/agent/tasks）直接放行，不占用额度。
func ChatGuardMiddleware(rateLimitSvc *service.RateLimitService) gin.HandlerFunc {
	return func(c *gin.Context) {
		path := c.Param("path") // /agent/*path → path = "/chat/stream"
		if !isChatPath(path) {
			c.Next()
			return
		}

		// 身份由上游 AuthMiddleware 注入；拿不到就交还给 Auth 处理（不要在这里 401）
		authCtx := GetAuthContext(c)
		if authCtx == nil || authCtx.UserUUID == "" || rateLimitSvc == nil {
			c.Next()
			return
		}

		// ---- 1) 频率 ----
		if err := rateLimitSvc.CheckChat(c.Request.Context(), authCtx.UserUUID); err != nil {
			writeGuardError(c, "RATE_LIMITED", "对话请求过于频繁，请稍后再试", err)
			return
		}

		// ---- 2) 并发（仅流式）----
		if isStreamPath(path) {
			release, err := rateLimitSvc.AcquireStreamSlot(c.Request.Context(), authCtx.UserUUID)
			if err != nil {
				writeGuardError(c, "STREAM_LIMIT_REACHED",
					"已有对话正在进行，请等待当前回答结束或停止生成后再试", err)
				return
			}
			defer release()
			slog.Debug("stream slot acquired",
				"user_uuid", authCtx.UserUUID, "request_id", GetRequestID(c))
		}

		c.Next()
	}
}

// isChatPath 判断是否对话类路径（/chat、/chat/stream、/chat/cancel）
func isChatPath(path string) bool {
	p := strings.TrimPrefix(path, "/")
	return p == "chat" || strings.HasPrefix(p, "chat/")
}

// isStreamPath 判断是否流式对话路径
func isStreamPath(path string) bool {
	p := strings.TrimPrefix(path, "/")
	return p == "chat/stream" || strings.HasPrefix(p, "chat/stream")
}

func writeGuardError(c *gin.Context, code, msg string, err error) {
	slog.Warn("Chat guard rejected",
		"code", code, "path", c.Param("path"),
		"request_id", GetRequestID(c), "err", err)
	c.AbortWithStatusJSON(http.StatusTooManyRequests, model.ErrorResponse{
		Code:      code,
		Message:   msg,
		RequestID: GetRequestID(c),
	})
}
