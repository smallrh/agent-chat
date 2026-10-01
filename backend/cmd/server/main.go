// cmd/server/main.go — Go Auth Service 入口
//
// 启动流程：
// 1. Load Config（从 .env / 环境变量）
// 2. Init DB（Postgres + Redis）
// 3. Init JWT TokenService（自动生成 RSA 密钥对）
// 4. Run golang-migrate（如有需要）
// 5. 组装 Repositories → Services → Handlers → Router
// 6. Listen on HTTP_PORT
//
// 关键架构：
// - auth_users 表（Go Auth Service 专属）
// - users 表（Python Agent 原有，不动）
// - 注册时同时确保 Python users 表有记录
// - Go → Python Agent 通过 /agent/* 代理，注入 X-User-Id
package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"agent/auth-service/internal/client"
	"agent/auth-service/internal/config"
	"agent/auth-service/internal/db"
	"agent/auth-service/internal/handler"
	"agent/auth-service/internal/repository"
	"agent/auth-service/internal/router"
	"agent/auth-service/internal/service"
)

func main() {
	// -------------------------------------------------------
	// 1. 加载配置
	// -------------------------------------------------------
	cfg, err := config.Load()
	if err != nil {
		slog.Error("Failed to load config", "err", err)
		os.Exit(1)
	}
	slog.Info("Config loaded",
		"app_env", cfg.AppEnv,
		"port", cfg.Port,
		"pg_host", cfg.PGHost,
		"redis_addr", cfg.RedisAddr(),
		"agent_url", cfg.AgentServiceURL,
	)

	// -------------------------------------------------------
	// 2. 初始化 DB + Redis
	// -------------------------------------------------------
	database, err := db.New(cfg)
	if err != nil {
		slog.Error("Failed to connect database", "err", err)
		os.Exit(1)
	}

	// -------------------------------------------------------
	// 3. JWT TokenService（自动生成密钥对）
	// -------------------------------------------------------
	tokenSvc, err := service.NewTokenService(cfg)
	if err != nil {
		slog.Error("Failed to init TokenService", "err", err)
		_ = database.Close()
		os.Exit(1)
	}

	// -------------------------------------------------------
	// 4. Repositories
	// -------------------------------------------------------
	userRepo := repository.NewAuthUserRepository(database.SQL)
	refreshRepo := repository.NewRefreshTokenRepository(database.SQL)
	pythonUserRepo := repository.NewPythonUserRepository(database.SQL)

	// -------------------------------------------------------
	// 5. Services
	// -------------------------------------------------------
	passwordSvc := service.NewPasswordService()
	rateLimitSvc := service.NewRateLimitService(database.Redis, cfg)
	authSvc := service.NewAuthService(cfg, userRepo, refreshRepo, pythonUserRepo,
		passwordSvc, tokenSvc, rateLimitSvc)

	// -------------------------------------------------------
	// 6. Handlers
	// -------------------------------------------------------
	authHandler := handler.NewAuthHandler(authSvc)
	healthHandler := handler.NewHealthHandler(database)

	// -------------------------------------------------------
	// 7. Agent Client（Go → Python 代理）
	// -------------------------------------------------------
	agentClient := client.NewAgentClient(cfg)

	// -------------------------------------------------------
	// 8. Router
	// -------------------------------------------------------
	r := router.New(authHandler, healthHandler, tokenSvc, agentClient, rateLimitSvc)

	// -------------------------------------------------------
	// 9. 优雅启动 + 优雅关闭
	// -------------------------------------------------------
	addr := fmt.Sprintf(":%d", cfg.Port)
	srv := &http.Server{
		Addr:         addr,
		Handler:      r.Handler(),
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	// 后台监听（不要阻塞 main）
	go func() {
		slog.Info("Go Auth Service starting", "addr", addr, "env", cfg.AppEnv)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("HTTP server error", "err", err)
			os.Exit(1)
		}
	}()

	// 等待优雅关闭信号
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	slog.Info("Shutting down...")

	// 顺序：先停 HTTP Server（停止接收新请求）→ 再关 Redis → 最后关 PostgreSQL
	// （参考 09-日志与可观测性.md 第 7 节 Graceful Shutdown）

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := srv.Shutdown(ctx); err != nil {
		slog.Error("Server shutdown failed", "err", err)
	}
	slog.Info("HTTP server stopped")

	if err := database.Close(); err != nil {
		slog.Error("Database close failed", "err", err)
	}
	slog.Info("Database + Redis closed")

	slog.Info("Service shutdown complete")
}
