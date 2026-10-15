package handler

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/config"
	"agent/auth-service/internal/middleware"
	"agent/auth-service/internal/model"
	"agent/auth-service/internal/repository"
	"agent/auth-service/internal/service"
)

// AdminHandler 管理后台 API（P3「管理后台 API：封禁用户、看用量」）
//
// 鉴权：请求头 X-Admin-Token 必须等于 config.AdminToken。
// 为什么用共享令牌而不是角色体系：
//   管理后台是低频内部操作，为它给 auth_users 加 role 列 + 改 JWT claims +
//   改注册流程，会牵动整条登录链路，风险远大于收益。
//   代价是不好在 UI 上做「管理员登录」，等真有第二个管理员再说。
//
// 安全默认值：AdminToken 为空 → **所有管理端点返回 404**（不是 401），
// 这样连「这个系统有管理接口」都不对外暴露。
type AdminHandler struct {
	users *repository.AuthUserRepository
	plans *repository.UserPlanRepository
	quota *service.QuotaService
	cfg   *config.Config
}

func NewAdminHandler(
	users *repository.AuthUserRepository,
	plans *repository.UserPlanRepository,
	quota *service.QuotaService,
	cfg *config.Config,
) *AdminHandler {
	return &AdminHandler{users: users, plans: plans, quota: quota, cfg: cfg}
}

// Guard 管理端点的统一鉴权中间件
func (h *AdminHandler) Guard(c *gin.Context) {
	if h.cfg.AdminToken == "" {
		// 未配置 → 端点不存在（404），而不是「没权限」（401）
		c.AbortWithStatusJSON(http.StatusNotFound, model.ErrorResponse{
			Code:    "NOT_FOUND",
			Message: "Not found",
		})
		return
	}
	if c.GetHeader("X-Admin-Token") != h.cfg.AdminToken {
		c.AbortWithStatusJSON(http.StatusUnauthorized, model.ErrorResponse{
			Code:    "UNAUTHORIZED",
			Message: "Invalid admin token",
		})
		return
	}
	c.Next()
}

// Usage 查某个用户的用量（含账号状态）
func (h *AdminHandler) Usage(c *gin.Context) {
	userUUID := c.Param("uuid")
	u, err := h.users.GetByUUID(c.Request.Context(), userUUID)
	if err != nil {
		c.AbortWithStatusJSON(http.StatusNotFound, model.ErrorResponse{
			Code:      "NOT_FOUND",
			Message:   "User not found",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}

	usage, err := h.quota.Usage(c.Request.Context(), userUUID)
	if err != nil {
		c.AbortWithStatusJSON(http.StatusInternalServerError, model.ErrorResponse{
			Code:      "INTERNAL_ERROR",
			Message:   "Failed to read usage",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}

	writeOK(c, http.StatusOK, model.AdminUsageResponse{
		UsageResponse: *usage,
		Username:      u.Username,
		Email:         u.Email,
		Status:        u.Status,
	})
}

// SetStatus 封禁 / 解封（1=正常 2=封禁）
func (h *AdminHandler) SetStatus(c *gin.Context) {
	var body struct {
		Status int16 `json:"status"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || (body.Status != 1 && body.Status != 2) {
		c.AbortWithStatusJSON(http.StatusBadRequest, model.ErrorResponse{
			Code:      "INVALID_REQUEST",
			Message:   "body: {\"status\": 1|2}（1=正常 2=封禁）",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}

	userUUID := c.Param("uuid")
	u, err := h.users.GetByUUID(c.Request.Context(), userUUID)
	if err != nil {
		c.AbortWithStatusJSON(http.StatusNotFound, model.ErrorResponse{
			Code:      "NOT_FOUND",
			Message:   "User not found",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}

	if err := h.users.UpdateStatus(c.Request.Context(), u.ID, body.Status); err != nil {
		c.AbortWithStatusJSON(http.StatusInternalServerError, model.ErrorResponse{
			Code:      "INTERNAL_ERROR",
			Message:   "Failed to update status",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}

	writeOK(c, http.StatusOK, gin.H{
		"user_uuid": userUUID,
		"status":    body.Status,
	})
}

// SetPlan 调整某个用户的套餐额度（写 user_plans 表）
func (h *AdminHandler) SetPlan(c *gin.Context) {
	var p model.UserPlan
	if err := c.ShouldBindJSON(&p); err != nil {
		c.AbortWithStatusJSON(http.StatusBadRequest, model.ErrorResponse{
			Code:      "INVALID_REQUEST",
			Message:   "invalid plan body",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}
	p.UserUUID = c.Param("uuid")
	if p.UserUUID == "" {
		c.AbortWithStatusJSON(http.StatusBadRequest, model.ErrorResponse{
			Code:    "INVALID_REQUEST",
			Message: "missing user uuid",
		})
		return
	}
	if p.PlanCode == "" {
		p.PlanCode = h.cfg.DefaultPlanCode
	}

	if err := h.plans.Upsert(c.Request.Context(), &p); err != nil {
		c.AbortWithStatusJSON(http.StatusInternalServerError, model.ErrorResponse{
			Code:      "INTERNAL_ERROR",
			Message:   "Failed to save plan",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}
	writeOK(c, http.StatusOK, p)
}

// ListPlans 列出所有被调整过额度的用户
func (h *AdminHandler) ListPlans(c *gin.Context) {
	items, err := h.plans.List(c.Request.Context(), 200)
	if err != nil {
		c.AbortWithStatusJSON(http.StatusInternalServerError, model.ErrorResponse{
			Code:      "INTERNAL_ERROR",
			Message:   "Failed to list plans",
			RequestID: middleware.GetRequestID(c),
		})
		return
	}
	writeOK(c, http.StatusOK, gin.H{"items": items})
}
