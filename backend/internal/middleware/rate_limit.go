package middleware

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/model"
	"agent/auth-service/internal/service"
)

// RateLimitMiddleware 登录/注册接口限流
func RateLimitMiddleware(rateLimitSvc *service.RateLimitService, kind string) gin.HandlerFunc {
	return func(c *gin.Context) {
		ip := c.ClientIP()

		var err error
		switch kind {
		case "login":
			// 先读 body 拿 username 可能比较复杂，这里只按 IP 限流
			err = rateLimitSvc.CheckLogin(c.Request.Context(), ip, "")
		case "register":
			err = rateLimitSvc.CheckRegister(c.Request.Context(), ip)
		default:
			c.Next()
			return
		}

		if err != nil {
			requestID, _ := c.Get("request_id")
			rid, _ := requestID.(string)
			c.AbortWithStatusJSON(http.StatusTooManyRequests, model.ErrorResponse{
				Code:      "RATE_LIMITED",
				Message:   "Too many requests, please try again later",
				RequestID: rid,
			})
			return
		}

		c.Next()
	}
}
