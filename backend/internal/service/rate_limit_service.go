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

// CheckChat 对话限流（按 user_uuid 固定窗口；通过后再计数）
func (s *RateLimitService) CheckChat(ctx context.Context, userUUID string) error {
	if userUUID == "" || s.cfg.ChatUserLimit <= 0 {
		return nil
	}
	key := fmt.Sprintf("chat:user:%s", userUUID)
	if err := s.check(ctx, key, s.cfg.ChatUserLimit, s.cfg.ChatUserWindow); err != nil {
		return err
	}
	s.incr(ctx, key, s.cfg.ChatUserWindow)
	return nil
}

// AcquireStreamSlot 占用一个「流式对话」并发额度（按 user_uuid 计数，Redis 全局共享）。
//
// 与 CheckChat 的关系：CheckChat 管**频率**（每分钟多少次），这里管**并发**（同时几条流）。
// 只靠频率挡不住「同时开 5 个标签页各问一句」——那会把 Agent 的连接池/线程池打满。
//
// 返回 release 函数，调用方必须 defer 执行；Redis 故障一律放行（与 check() 同策略）。
func (s *RateLimitService) AcquireStreamSlot(ctx context.Context, userUUID string) (func(), error) {
	noop := func() {}
	if userUUID == "" || s.cfg.ChatStreamConcurrency <= 0 {
		return noop, nil
	}

	key := fmt.Sprintf("chat:stream:%s", userUUID)
	count, err := s.rdb.Incr(ctx, key).Result()
	if err != nil {
		slog.Warn("StreamSlot incr error (degrade to allow)", "key", key, "err", err)
		return noop, nil
	}
	// 兜底 TTL：进程被 kill 时没走到 release，计数靠 TTL 自动清零
	_ = s.rdb.Expire(ctx, key, s.cfg.ChatStreamTTL).Err()

	if int(count) > s.cfg.ChatStreamConcurrency {
		// 超限：立刻归还本次 incr
		_, _ = s.rdb.Decr(ctx, key).Result()
		slog.Warn("Concurrent stream limit exceeded",
			"user_uuid", userUUID, "count", count, "limit", s.cfg.ChatStreamConcurrency)
		return noop, errors.ErrStreamLimitReached
	}

	var released bool
	release := func() {
		if released {
			return
		}
		released = true
		// 请求 ctx 此时可能已取消/超时，用独立短 ctx 归还，避免计数泄漏
		rctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if _, err := s.rdb.Decr(rctx, key).Result(); err != nil {
			slog.Warn("StreamSlot decr error", "key", key, "err", err)
		}
	}
	return release, nil
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
