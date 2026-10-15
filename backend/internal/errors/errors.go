package errors

import (
	"errors"
	"fmt"
	"net/http"
)

// AppError 统一错误结构
type AppError struct {
	Code    string
	Message string
	Status  int
	Err     error // 原始错误（不暴露给客户端）
}

func (e *AppError) Error() string {
	if e.Err != nil {
		return fmt.Sprintf("[%s] %s: %v", e.Code, e.Message, e.Err)
	}
	return fmt.Sprintf("[%s] %s", e.Code, e.Message)
}

// Unwrap 支持 errors.Is / errors.As
func (e *AppError) Unwrap() error { return e.Err }

// ------------------------------------------------------------
// 预定义错误码
// ------------------------------------------------------------

var (
	// 400 Bad Request
	ErrInvalidRequest = &AppError{
		Code: "INVALID_REQUEST", Message: "Invalid request parameters", Status: http.StatusBadRequest,
	}

	// 401 Unauthorized
	ErrUnauthorized = &AppError{
		Code: "UNAUTHORIZED", Message: "Unauthorized", Status: http.StatusUnauthorized,
	}
	ErrInvalidCredentials = &AppError{
		Code: "INVALID_CREDENTIALS", Message: "Invalid username or password", Status: http.StatusUnauthorized,
	}
	ErrTokenExpired = &AppError{
		Code: "TOKEN_EXPIRED", Message: "Token has expired", Status: http.StatusUnauthorized,
	}
	ErrInvalidToken = &AppError{
		Code: "INVALID_TOKEN", Message: "Invalid token", Status: http.StatusUnauthorized,
	}

	// 403 Forbidden
	ErrForbidden = &AppError{
		Code: "FORBIDDEN", Message: "Access forbidden", Status: http.StatusForbidden,
	}
	ErrUserDisabled = &AppError{
		Code: "USER_DISABLED", Message: "Account is disabled", Status: http.StatusForbidden,
	}

	// 404 Not Found
	ErrNotFound = &AppError{
		Code: "NOT_FOUND", Message: "Resource not found", Status: http.StatusNotFound,
	}

	// 409 Conflict
	ErrUsernameTaken = &AppError{
		Code: "USERNAME_TAKEN", Message: "Username already exists", Status: http.StatusConflict,
	}
	ErrEmailTaken = &AppError{
		Code: "EMAIL_TAKEN", Message: "Email already exists", Status: http.StatusConflict,
	}

	// 429 Too Many Requests
	ErrRateLimited = &AppError{
		Code: "RATE_LIMITED", Message: "Too many requests, please try again later", Status: http.StatusTooManyRequests,
	}
	// 单用户并发 SSE 数已达上限（同属 429，但错误码不同，便于前端区分提示）
	ErrStreamLimitReached = &AppError{
		Code:    "STREAM_LIMIT_REACHED",
		Message: "Too many concurrent streams, please wait for the current one to finish",
		Status:  http.StatusTooManyRequests,
	}
	// P3 计费：当日配额用尽。
	// 同属 429 但语义与限流完全不同：限流是「太快了，等一会」，
	// 配额是「今天的额度用完了，明天再来或升级套餐」——前端要分开提示。
	ErrQuotaExceeded = &AppError{
		Code:    "QUOTA_EXCEEDED",
		Message: "Daily quota exceeded, please upgrade your plan or try again tomorrow",
		Status:  http.StatusTooManyRequests,
	}

	// 500 Internal Server Error
	ErrInternal = &AppError{
		Code: "INTERNAL_ERROR", Message: "Internal server error", Status: http.StatusInternalServerError,
	}

	// Agent Proxy 相关
	ErrAgentBadGateway = &AppError{
		Code: "BAD_GATEWAY", Message: "Agent service is unavailable", Status: http.StatusBadGateway,
	}
	ErrAgentTimeout = &AppError{
		Code: "GATEWAY_TIMEOUT", Message: "Agent service timed out", Status: http.StatusGatewayTimeout,
	}
)

// Wrap 把原始错误包装进 AppError
func Wrap(base *AppError, err error) *AppError {
	if err == nil {
		return base
	}
	return &AppError{
		Code:    base.Code,
		Message: base.Message,
		Status:  base.Status,
		Err:     err,
	}
}

// Is 判断是否为特定 AppError 类型（检查 Code + Status）
func Is(err error, target *AppError) bool {
	var ae *AppError
	if errors.As(err, &ae) {
		return ae.Code == target.Code && ae.Status == target.Status
	}
	return false
}
