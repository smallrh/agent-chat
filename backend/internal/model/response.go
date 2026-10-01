package model

// APIResponse 统一响应结构
type APIResponse struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Data      any    `json:"data,omitempty"`
	RequestID string `json:"request_id,omitempty"`
}

// ErrorResponse 错误响应（带 request_id 方便排查）
type ErrorResponse struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	RequestID string `json:"request_id,omitempty"`
}

// AuthContext 认证中间件注入的上下文信息
type AuthContext struct {
	UserUUID string
	AuthUser *AuthUser
}
