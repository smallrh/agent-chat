package middleware

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/model"
	"agent/auth-service/internal/service"
)

// AuthMiddleware JWT 验证中间件
// 成功后把 AuthContext 注入 gin.Context
func AuthMiddleware(tokenSvc *service.TokenService) gin.HandlerFunc {
	return func(c *gin.Context) {
		authHeader := c.GetHeader("Authorization")
		if authHeader == "" {
			unauthorized(c, "Missing Authorization header")
			return
		}

		// 必须是 "Bearer " 前缀
		parts := strings.SplitN(authHeader, " ", 2)
		if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") {
			unauthorized(c, "Invalid Authorization format, expected 'Bearer <token>'")
			return
		}

		tokenString := parts[1]

		// 严格验证：signature + exp + iss + algorithm
		claims, err := tokenSvc.VerifyAccess(tokenString)
		if err != nil {
			unauthorized(c, "Invalid or expired token")
			return
		}

		// sub 就是 user_uuid（跨服务身份）
		userUUID := claims.Subject
		if userUUID == "" {
			unauthorized(c, "Token has no subject")
			return
		}

		// 注入 AuthContext
		c.Set("auth_context", &model.AuthContext{
			UserUUID: userUUID,
		})

		c.Next()
	}
}

// GetAuthContext 从 gin.Context 取认证信息
func GetAuthContext(c *gin.Context) *model.AuthContext {
	if v, ok := c.Get("auth_context"); ok {
		if ctx, ok := v.(*model.AuthContext); ok {
			return ctx
		}
	}
	return nil
}

// unauthorized 返回统一 401
func unauthorized(c *gin.Context, msg string) {
	requestID, _ := c.Get("request_id")
	rid, _ := requestID.(string)
	c.AbortWithStatusJSON(http.StatusUnauthorized, model.ErrorResponse{
		Code:      "UNAUTHORIZED",
		Message:   msg,
		RequestID: rid,
	})
}
