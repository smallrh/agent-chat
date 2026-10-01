package handler

import (
	stderrs "errors"
	"io"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	apperrs "agent/auth-service/internal/errors"
	"agent/auth-service/internal/middleware"
	"agent/auth-service/internal/model"
	"agent/auth-service/internal/service"
)

// AuthHandler 认证相关 HTTP Handler
type AuthHandler struct {
	authSvc *service.AuthService
}

// NewAuthHandler 构造
func NewAuthHandler(authSvc *service.AuthService) *AuthHandler {
	return &AuthHandler{authSvc: authSvc}
}

// ------------------------------------------------------------
// 统一错误响应
// ------------------------------------------------------------

func writeError(c *gin.Context, err error) {
	requestID, _ := c.Get("request_id")
	rid, _ := requestID.(string)

	var appErr *apperrs.AppError
	if stderrs.As(err, &appErr) {
		slog.Warn("Auth handler error",
			"code", appErr.Code, "msg", appErr.Message,
			"request_id", rid,
			"err_detail", appErr.Err,
		)
		c.JSON(appErr.Status, model.ErrorResponse{
			Code:      appErr.Code,
			Message:   appErr.Message,
			RequestID: rid,
		})
		return
	}

	slog.Error("Unexpected handler error", "err", err, "request_id", rid)
	c.JSON(http.StatusInternalServerError, model.ErrorResponse{
		Code:      "INTERNAL_ERROR",
		Message:   "Internal server error",
		RequestID: rid,
	})
}

func writeOK(c *gin.Context, status int, data any) {
	requestID, _ := c.Get("request_id")
	rid, _ := requestID.(string)
	c.JSON(status, model.APIResponse{
		Code:      "OK",
		Message:   "Success",
		Data:      data,
		RequestID: rid,
	})
}

// ------------------------------------------------------------
// Register
// ------------------------------------------------------------

// Register POST /api/v1/auth/register
func (h *AuthHandler) Register(c *gin.Context) {
	var req service.RegisterRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		writeError(c, apperrs.ErrInvalidRequest)
		return
	}

	req.IP = c.ClientIP()

	resp, err := h.authSvc.Register(c.Request.Context(), &req)
	if err != nil {
		writeError(c, err)
		return
	}

	writeOK(c, http.StatusCreated, resp)
}

// ------------------------------------------------------------
// Login
// ------------------------------------------------------------

// Login POST /api/v1/auth/login
func (h *AuthHandler) Login(c *gin.Context) {
	var req service.LoginRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		writeError(c, apperrs.ErrInvalidRequest)
		return
	}

	req.IP = c.ClientIP()

	resp, err := h.authSvc.Login(c.Request.Context(), &req)
	if err != nil {
		writeError(c, err)
		return
	}

	writeOK(c, http.StatusOK, resp)
}

// ------------------------------------------------------------
// Refresh
// ------------------------------------------------------------

// Refresh POST /api/v1/auth/refresh
func (h *AuthHandler) Refresh(c *gin.Context) {
	var req service.RefreshRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		writeError(c, apperrs.ErrInvalidRequest)
		return
	}

	resp, err := h.authSvc.Refresh(c.Request.Context(), &req)
	if err != nil {
		writeError(c, err)
		return
	}

	writeOK(c, http.StatusOK, resp)
}

// ------------------------------------------------------------
// Logout
// ------------------------------------------------------------

// Logout POST /api/v1/auth/logout（需要 Authorization）
func (h *AuthHandler) Logout(c *gin.Context) {
	ctx := middleware.GetAuthContext(c)
	if ctx == nil {
		writeError(c, apperrs.ErrUnauthorized)
		return
	}

	var body struct {
		RefreshToken string `json:"refresh_token"`
	}
	// refresh_token 是可选的：不传 → 撤销该用户所有 Refresh Token；传了 → 只撤销这一个
	// io.EOF = 空 body，正常；其他 JSON 格式错误返回 400
	if err := c.ShouldBindJSON(&body); err != nil && !stderrs.Is(err, io.EOF) {
		writeError(c, apperrs.ErrInvalidRequest)
		return
	}

	if err := h.authSvc.Logout(c.Request.Context(), ctx.UserUUID, body.RefreshToken); err != nil {
		writeError(c, err)
		return
	}

	writeOK(c, http.StatusOK, gin.H{"message": "Logged out successfully"})
}

// ------------------------------------------------------------
// Me
// ------------------------------------------------------------

// Me GET /api/v1/auth/me（从 JWT sub 获取 user_uuid，不接受 query/body）
func (h *AuthHandler) Me(c *gin.Context) {
	ctx := middleware.GetAuthContext(c)
	if ctx == nil || ctx.UserUUID == "" {
		writeError(c, apperrs.ErrUnauthorized)
		return
	}

	user, err := h.authSvc.Me(c.Request.Context(), ctx.UserUUID)
	if err != nil {
		writeError(c, err)
		return
	}

	writeOK(c, http.StatusOK, user)
}
