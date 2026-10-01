package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	stderrs "errors"
	"log/slog"
	"time"

	apperrs "agent/auth-service/internal/errors"
	"agent/auth-service/internal/model"
	"agent/auth-service/internal/repository"

	"github.com/google/uuid"
)

// refreshTokenHash 对明文 Refresh Token 做 SHA256 hex
// Refresh Token 是 crypto/rand 生成的 128-bit 随机串（32 字节 hex），
// preimage 完全不可能，SHA256 是合适选择（argon2id 是为人类密码设计的慢速哈希，不适用）
// 64 hex 字符，直接存 refresh_tokens.token_hash（UNIQUE）
func refreshTokenHash(plain string) string {
	h := sha256.Sum256([]byte(plain))
	return hex.EncodeToString(h[:])
}

// AuthService 认证业务顶层编排（Register/Login/Refresh/Logout/Me）
type AuthService struct {
	cfg             interface{}
	userRepo        *repository.AuthUserRepository
	refreshRepo     *repository.RefreshTokenRepository
	pythonUserRepo  *repository.PythonUserRepository
	passwordService *PasswordService
	tokenService    *TokenService
	rateLimitSvc    *RateLimitService
}

// NewAuthService 构造
func NewAuthService(
	cfg interface{},
	userRepo *repository.AuthUserRepository,
	refreshRepo *repository.RefreshTokenRepository,
	pythonUserRepo *repository.PythonUserRepository,
	passwordService *PasswordService,
	tokenService *TokenService,
	rateLimitSvc *RateLimitService,
) *AuthService {
	return &AuthService{
		cfg:             cfg,
		userRepo:        userRepo,
		refreshRepo:     refreshRepo,
		pythonUserRepo:  pythonUserRepo,
		passwordService: passwordService,
		tokenService:    tokenService,
		rateLimitSvc:    rateLimitSvc,
	}
}

// ------------------------------------------------------------
// 注册
// ------------------------------------------------------------

// RegisterRequest 注册请求
type RegisterRequest struct {
	Username string `json:"username" binding:"required,min=3,max=128"`
	Email    string `json:"email" binding:"required,email"`
	Password string `json:"password" binding:"required,min=8,max=256"`
	IP       string `json:"-"`
}

// RegisterResponse 注册响应
type RegisterResponse struct {
	User model.AuthUserPublic `json:"user"`
}

// Register 创建新用户（auth_users + 确保 Python users 表有记录）
func (s *AuthService) Register(ctx context.Context, req *RegisterRequest) (*RegisterResponse, error) {
	if err := s.rateLimitSvc.CheckRegister(ctx, req.IP); err != nil {
		return nil, err
	}

	hash, err := s.passwordService.Hash(req.Password)
	if err != nil {
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}

	authUserUUID := uuid.New().String()

	user := &model.AuthUser{
		AuthUserUUID: authUserUUID,
		Username:     req.Username,
		Email:        req.Email,
		PasswordHash: hash,
		Status:       1,
	}

	if err := s.userRepo.Create(ctx, user); err != nil {
		if apperrs.Is(err, apperrs.ErrUsernameTaken) || apperrs.Is(err, apperrs.ErrEmailTaken) {
			return nil, err
		}
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}

	if err := s.pythonUserRepo.EnsureUser(ctx, authUserUUID); err != nil {
		slog.Warn("Failed to ensure Python user record (non-fatal)",
			"user_uuid", authUserUUID, "err", err)
	}

	slog.Info("User registered", "user_uuid", authUserUUID, "username", req.Username)
	return &RegisterResponse{User: user.ToPublic()}, nil
}

// ------------------------------------------------------------
// 登录
// ------------------------------------------------------------

// LoginRequest 登录请求
type LoginRequest struct {
	Username string `json:"username" binding:"required"`
	Password string `json:"password" binding:"required"`
	IP       string `json:"-"`
}

// LoginResponse 登录响应
type LoginResponse struct {
	AccessToken  string               `json:"access_token"`
	TokenType    string               `json:"token_type"`
	ExpiresIn    int64                `json:"expires_in"`
	RefreshToken string               `json:"refresh_token"`
	User         model.AuthUserPublic `json:"user"`
}

// Login 用户登录 → 返回 Access + Refresh Token
func (s *AuthService) Login(ctx context.Context, req *LoginRequest) (*LoginResponse, error) {
	if err := s.rateLimitSvc.CheckLogin(ctx, req.IP, req.Username); err != nil {
		return nil, err
	}

	user, err := s.userRepo.GetByUsername(ctx, req.Username)
	if err != nil {
		s.rateLimitSvc.RecordLoginFailed(ctx, req.IP, req.Username)
		slog.Warn("Login: user not found", "username", req.Username, "ip", req.IP)
		return nil, apperrs.ErrInvalidCredentials
	}

	match, err := s.passwordService.Verify(req.Password, user.PasswordHash)
	if err != nil || !match {
		s.rateLimitSvc.RecordLoginFailed(ctx, req.IP, req.Username)
		slog.Warn("Login: password mismatch", "user_uuid", user.AuthUserUUID, "ip", req.IP)
		return nil, apperrs.ErrInvalidCredentials
	}

	if !user.IsActive() {
		slog.Warn("Login: user disabled", "user_uuid", user.AuthUserUUID)
		return nil, apperrs.ErrUserDisabled
	}

	accessToken, accessExp, err := s.tokenService.IssueAccess(user.AuthUserUUID)
	if err != nil {
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}

	refreshPlain, _, refreshExp, err := s.tokenService.IssueRefresh()
	if err != nil {
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}

	rt := &model.RefreshToken{
		AuthUserID: user.ID,
		TokenHash:  refreshTokenHash(refreshPlain),
		ExpiresAt:  refreshExp,
	}
	if err := s.refreshRepo.Create(ctx, rt); err != nil {
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}

	slog.Info("User login success", "user_uuid", user.AuthUserUUID, "ip", req.IP)

	return &LoginResponse{
		AccessToken:  accessToken,
		TokenType:    "Bearer",
		ExpiresIn:    int64(accessExp.Sub(time.Now()).Seconds()),
		RefreshToken: refreshPlain,
		User:         user.ToPublic(),
	}, nil
}

// ------------------------------------------------------------
// Refresh Token 查找（明文 → SHA256 hash 直查 DB）
// ------------------------------------------------------------

// findRefreshTokenByPlain 把明文 Refresh Token 做 SHA256，直查 token_hash 列
// Refresh Token 是 crypto/rand 生成的 128-bit 随机串 + SHA256 = 足够强的单向性
func (s *AuthService) findRefreshTokenByPlain(ctx context.Context, plain string) (*model.RefreshToken, error) {
	return s.refreshRepo.GetByHash(ctx, refreshTokenHash(plain))
}

// ------------------------------------------------------------
// Refresh
// ------------------------------------------------------------

// RefreshRequest 刷新请求
type RefreshRequest struct {
	RefreshToken string `json:"refresh_token" binding:"required"`
}

// RefreshResponse 刷新响应
type RefreshResponse struct {
	AccessToken  string `json:"access_token"`
	TokenType    string `json:"token_type"`
	ExpiresIn    int64  `json:"expires_in"`
	RefreshToken string `json:"refresh_token"`
}

// RefreshTokenRotation 刷新 Token（Rotation + Replay 检测）
// 旧 token revoke + 新 token create 在同一个事务里原子完成
func (s *AuthService) Refresh(ctx context.Context, req *RefreshRequest) (*RefreshResponse, error) {
	tokenRecord, err := s.findRefreshTokenByPlain(ctx, req.RefreshToken)
	if err != nil {
		return nil, err
	}

	if tokenRecord.IsExpired() {
		_ = s.refreshRepo.Revoke(ctx, tokenRecord.ID)
		return nil, apperrs.ErrTokenExpired
	}

	if tokenRecord.IsRevoked() {
		slog.Warn("Refresh Token reuse detected!",
			"auth_user_id", tokenRecord.AuthUserID,
			"token_id", tokenRecord.ID)
		_ = s.refreshRepo.RevokeAllForUser(ctx, tokenRecord.AuthUserID)
		return nil, apperrs.ErrInvalidToken
	}

	user, err := s.userRepo.GetByID(ctx, tokenRecord.AuthUserID)
	if err != nil {
		return nil, apperrs.ErrInvalidToken
	}

	if !user.IsActive() {
		return nil, apperrs.ErrUserDisabled
	}

	// 先准备好新 Refresh Token 的所有内容（DB 操作之前）
	accessToken, accessExp, err := s.tokenService.IssueAccess(user.AuthUserUUID)
	if err != nil {
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}

	newRefreshPlain, _, newRefreshExp, err := s.tokenService.IssueRefresh()
	if err != nil {
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}

	var parentID *int64 = &tokenRecord.ID
	newRT := &model.RefreshToken{
		AuthUserID:    user.ID,
		TokenHash:     refreshTokenHash(newRefreshPlain),
		ParentTokenID: parentID,
		ExpiresAt:     newRefreshExp,
	}

	// ---- 事务：旧 Token 撤销 + 新 Token 创建 原子完成 ----
	if err := s.refreshRepo.Rotate(ctx, tokenRecord.ID, newRT); err != nil {
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}

	slog.Info("Token refreshed (rotation)", "user_uuid", user.AuthUserUUID)

	return &RefreshResponse{
		AccessToken:  accessToken,
		TokenType:    "Bearer",
		ExpiresIn:    int64(accessExp.Sub(time.Now()).Seconds()),
		RefreshToken: newRefreshPlain,
	}, nil
}

// ------------------------------------------------------------
// Logout
// ------------------------------------------------------------

// Logout 撤销用户所有 Refresh Tokens
// refreshPlain 为空 → 撤销该用户所有 Refresh Token；否则只撤销指定的
func (s *AuthService) Logout(ctx context.Context, userUUID string, refreshPlain string) error {
	user, err := s.userRepo.GetByUUID(ctx, userUUID)
	if err != nil {
		return apperrs.ErrNotFound
	}

	if refreshPlain != "" {
		rt, err := s.findRefreshTokenByPlain(ctx, refreshPlain)
		if err == nil && rt != nil {
			return s.refreshRepo.Revoke(ctx, rt.ID)
		}
		return nil // 指定 token 不存在/已撤销 → 视为成功（幂等）
	}

	slog.Info("Logout: revoke all refresh tokens", "user_uuid", userUUID)
	return s.refreshRepo.RevokeAllForUser(ctx, user.ID)
}

// ------------------------------------------------------------
// Me
// ------------------------------------------------------------

// Me 根据 auth_user_uuid 返回用户信息
func (s *AuthService) Me(ctx context.Context, userUUID string) (*model.AuthUserPublic, error) {
	user, err := s.userRepo.GetByUUID(ctx, userUUID)
	if err != nil {
		return nil, err
	}
	return &model.AuthUserPublic{
		UserUUID: user.AuthUserUUID,
		Username: user.Username,
		Email:    user.Email,
	}, nil
}

// 确保 stderrs 和 time 被使用（避免未使用 import）
var _ = stderrs.Is
var _ = time.Now
