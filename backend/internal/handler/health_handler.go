package handler

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/db"
	"agent/auth-service/internal/model"
)

// HealthHandler 健康检查 Handler
type HealthHandler struct {
	database *db.Database
}

// NewHealthHandler 构造
func NewHealthHandler(database *db.Database) *HealthHandler {
	return &HealthHandler{database: database}
}

// Health GET /health
// 进程活着就行
func (h *HealthHandler) Health(c *gin.Context) {
	c.JSON(http.StatusOK, model.APIResponse{
		Code:    "OK",
		Message: "Service is running",
		Data: gin.H{
			"timestamp": time.Now().UTC().Format(time.RFC3339),
		},
	})
}

// Ready GET /ready
// 关键依赖都可用（Postgres + Redis）
func (h *HealthHandler) Ready(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
	defer cancel()

	if h.database == nil || !h.database.Ready(ctx) {
		c.JSON(http.StatusServiceUnavailable, model.ErrorResponse{
			Code:    "NOT_READY",
			Message: "Database or Redis not ready",
		})
		return
	}

	c.JSON(http.StatusOK, model.APIResponse{
		Code:    "OK",
		Message: "All dependencies ready",
	})
}
