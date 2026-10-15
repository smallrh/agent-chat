package handler

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/middleware"
	"agent/auth-service/internal/model"
	"agent/auth-service/internal/service"
)

// UsageHandler 用量查询：GET /api/v1/usage/me
//
// 让用户能看见自己用了多少、还剩多少 —— 计费功能如果没有自助查询，
// 用户只会在某天突然被 429 拦住而完全不知道为什么，支持工单会爆。
type UsageHandler struct {
	quota *service.QuotaService
}

func NewUsageHandler(quota *service.QuotaService) *UsageHandler {
	return &UsageHandler{quota: quota}
}

// Me 当前用户的今日/本月用量与配额
func (h *UsageHandler) Me(c *gin.Context) {
	authCtx := middleware.GetAuthContext(c)
	if authCtx == nil || authCtx.UserUUID == "" {
		c.AbortWithStatusJSON(http.StatusUnauthorized, model.ErrorResponse{
			Code:      "UNAUTHORIZED",
			Message:   "Authentication required",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}

	usage, err := h.quota.Usage(c.Request.Context(), authCtx.UserUUID)
	if err != nil {
		c.AbortWithStatusJSON(http.StatusInternalServerError, model.ErrorResponse{
			Code:      "INTERNAL_ERROR",
			Message:   "Failed to read usage",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}
	writeOK(c, http.StatusOK, usage)
}
