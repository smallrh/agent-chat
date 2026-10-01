package db

import (
	"context"
	"log/slog"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib" // 注册 pgx/v5 的 stdlib 驱动，驱动名是 "pgx"
	"github.com/jmoiron/sqlx"
	"github.com/redis/go-redis/v9"

	"agent/auth-service/internal/config"
)

// Database 聚合 SQLX + Redis 两个连接
type Database struct {
	SQL   *sqlx.DB
	Redis *redis.Client
	Cfg   *config.Config
}

// New 初始化 DB 连接（Postgres + Redis）
func New(cfg *config.Config) (*Database, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	// -------------------------------------------------------
	// PostgreSQL（pgx/v5 通过 _ import 自动注册 "pgx" 驱动）
	// -------------------------------------------------------
	sqlDB, err := sqlx.ConnectContext(ctx, "pgx", cfg.DSN())
	if err != nil {
		return nil, err
	}

	sqlDB.SetMaxOpenConns(20)
	sqlDB.SetMaxIdleConns(5)
	sqlDB.SetConnMaxLifetime(30 * time.Minute)
	sqlDB.SetConnMaxIdleTime(5 * time.Minute)

	if err := sqlDB.PingContext(ctx); err != nil {
		return nil, err
	}
	slog.Info("PostgreSQL connected", "host", cfg.PGHost, "db", cfg.PGDatabase)

	// -------------------------------------------------------
	// Redis
	// -------------------------------------------------------
	rdb := redis.NewClient(&redis.Options{
		Addr:         cfg.RedisAddr(),
		Password:     cfg.RedisPassword,
		DB:           cfg.RedisDB,
		PoolSize:     10,
		MinIdleConns: 3,
	})
	if err := rdb.Ping(ctx).Err(); err != nil {
		return nil, err
	}
	slog.Info("Redis connected", "addr", cfg.RedisAddr())

	return &Database{
		SQL:   sqlDB,
		Redis: rdb,
		Cfg:   cfg,
	}, nil
}

// Close 关闭所有连接
func (d *Database) Close() error {
	if d.Redis != nil {
		_ = d.Redis.Close()
	}
	if d.SQL != nil {
		return d.SQL.Close()
	}
	return nil
}

// Ready 用于 /ready 健康检查
func (d *Database) Ready(ctx context.Context) bool {
	if d.SQL == nil || d.Redis == nil {
		return false
	}
	if err := d.SQL.PingContext(ctx); err != nil {
		return false
	}
	if err := d.Redis.Ping(ctx).Err(); err != nil {
		return false
	}
	return true
}
