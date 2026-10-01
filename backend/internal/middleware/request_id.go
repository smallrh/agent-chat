package middleware

import (
	"context"
	"log/slog"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

// RequestIDMiddleware 为每个请求生成 / 校验 X-Request-Id
// 规则：
//   - 客户端没传 → 生成新 UUID
//   - 客户端传了 → 校验格式，不合格则重新生成
func RequestIDMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		requestID := c.GetHeader("X-Request-Id")

		// 校验 UUID 格式
		if requestID != "" {
			if _, err := uuid.Parse(requestID); err != nil {
				slog.Debug("Invalid X-Request-Id, regenerating", "client_value", requestID)
				requestID = ""
			}
		}

		if requestID == "" {
			requestID = uuid.New().String()
		}

		c.Set("request_id", requestID)
		c.Header("X-Request-Id", requestID)
		c.Next()
	}
}

// GetRequestID 从 gin.Context 取 request_id
func GetRequestID(c *gin.Context) string {
	if v, ok := c.Get("request_id"); ok {
		return v.(string)
	}
	return ""
}

// GetRequestIDFromContext 从标准 context.Context 取（供 client 层使用）
type requestIDKey struct{}

func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey{}, id)
}

func GetRequestIDFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(requestIDKey{}).(string); ok {
		return v
	}
	return ""
}

// LoggingMiddleware 结构化请求日志
func LoggingMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		c.Next()

		latencyMs := time.Since(start).Milliseconds()
		requestID, _ := c.Get("request_id")
		slog.Info("HTTP request",
			"method", c.Request.Method,
			"path", c.Request.URL.Path,
			"status", c.Writer.Status(),
			"latency_ms", latencyMs,
			"request_id", requestID,
			"ip", c.ClientIP(),
			"body_bytes", c.Writer.Size(),
		)
	}
}

// CORSMiddleware 根据配置允许的 Origins
// 如果 Auth Service 只被 BFF / App 内部调用，这里可以设为空
func CORSMiddleware(allowedOrigins []string) gin.HandlerFunc {
	originSet := make(map[string]struct{}, len(allowedOrigins))
	for _, o := range allowedOrigins {
		originSet[o] = struct{}{}
	}

	return func(c *gin.Context) {
		origin := c.GetHeader("Origin")

		// 精确匹配
		if _, ok := originSet[origin]; ok {
			c.Header("Access-Control-Allow-Origin", origin)
			c.Header("Access-Control-Allow-Credentials", "true")
			c.Header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
			c.Header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Request-Id")
			c.Header("Access-Control-Expose-Headers", "X-Request-Id")
		}

		if c.Request.Method == http.MethodOptions {
			c.AbortWithStatus(http.StatusNoContent)
			return
		}
		c.Next()
	}
}
