package service

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"

	"agent/auth-service/internal/config"
	"agent/auth-service/internal/errors"
)

// RateLimitService Redis 限流
type RateLimitService struct {
	rdb *redis.Client
	cfg *config.Config
}

// NewRateLimitService 构造
func NewRateLimitService(rdb *redis.Client, cfg *config.Config) *RateLimitService {
	return &RateLimitService{rdb: rdb, cfg: cfg}
}

// CheckLogin 登录限流（IP + username）
// 返回：(是否通过, 错误)
func (s *RateLimitService) CheckLogin(ctx context.Context, ip, username string) error {
	if err := s.check(ctx, fmt.Sprintf("login:ip:%s", ip), s.cfg.LoginIPLimit, s.cfg.LoginIPWindow); err != nil {
		return err
	}
	if username != "" {
		if err := s.check(ctx, fmt.Sprintf("login:user:%s", username), s.cfg.LoginUserLimit, s.cfg.LoginUserWindow); err != nil {
			return err
		}
	}
	return nil
}

// CheckRegister 注册限流（IP）
func (s *RateLimitService) CheckRegister(ctx context.Context, ip string) error {
	return s.check(ctx, fmt.Sprintf("register:ip:%s", ip), s.cfg.RegisterIPLimit, s.cfg.RegisterIPWindow)
}

// RecordLoginFailed 记录登录失败（用于限流计数）
func (s *RateLimitService) RecordLoginFailed(ctx context.Context, ip, username string) {
	s.incr(ctx, fmt.Sprintf("login:ip:%s", ip), s.cfg.LoginIPWindow)
	if username != "" {
		s.incr(ctx, fmt.Sprintf("login:user:%s", username), s.cfg.LoginUserWindow)
	}
}

// ------------------------------------------------------------
// 内部实现：固定窗口限流（简单可靠，适合登录场景）
// ------------------------------------------------------------

func (s *RateLimitService) check(ctx context.Context, key string, limit int, window time.Duration) error {
	count, err := s.rdb.Get(ctx, key).Int()
	if err != nil && err != redis.Nil {
		slog.Warn("RateLimit get error", "key", key, "err", err)
		return nil // Redis 故障时放行，避免级联阻断
	}
	if count >= limit {
		slog.Warn("Rate limit exceeded", "key", key, "count", count, "limit", limit)
		return errors.ErrRateLimited
	}
	return nil
}

func (s *RateLimitService) incr(ctx context.Context, key string, window time.Duration) {
	pipe := s.rdb.TxPipeline()
	incr := pipe.Incr(ctx, key)
	pipe.Expire(ctx, key, window)
	if _, err := pipe.Exec(ctx); err != nil {
		slog.Warn("RateLimit incr error", "key", key, "err", err)
		return
	}
	// 如果 incr 是第一次，设置过期
	if incr.Val() == 1 {
		_ = s.rdb.Expire(ctx, key, window).Err()
	}
}
