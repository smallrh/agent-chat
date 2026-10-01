package repository

import (
	"context"
	"database/sql"
	stderrs "errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jmoiron/sqlx"

	apperrs "agent/auth-service/internal/errors"
	"agent/auth-service/internal/model"
)

// AuthUserRepository 认证用户数据访问层
type AuthUserRepository struct {
	db *sqlx.DB
}

// NewAuthUserRepository 构造
func NewAuthUserRepository(db *sqlx.DB) *AuthUserRepository {
	return &AuthUserRepository{db: db}
}

// Create 插入新用户（auth_users 表）
func (r *AuthUserRepository) Create(ctx context.Context, user *model.AuthUser) error {
	query := `
		INSERT INTO auth_users
			(auth_user_uuid, username, email, password_hash, status)
		VALUES
			($1, $2, $3, $4, $5)
		RETURNING id, created_at, updated_at
	`

	err := r.db.QueryRowContext(ctx, query,
		user.AuthUserUUID, user.Username, user.Email,
		user.PasswordHash, user.Status,
	).Scan(&user.ID, &user.CreatedAt, &user.UpdatedAt)

	if err != nil {
		var pgErr *pgconn.PgError
		if stderrs.As(err, &pgErr) && pgErr.Code == "23505" {
			return classifyUniqueViolation(pgErr.ConstraintName)
		}
		slog.Error("auth_users Create failed", "err", err)
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return nil
}

// GetByUsername 根据用户名查用户
func (r *AuthUserRepository) GetByUsername(ctx context.Context, username string) (*model.AuthUser, error) {
	var u model.AuthUser
	err := r.db.GetContext(ctx, &u, `
		SELECT id, auth_user_uuid, username, email, password_hash, status, created_at, updated_at
		FROM auth_users WHERE username = $1 LIMIT 1
	`, username)
	if err != nil {
		if stderrs.Is(err, sql.ErrNoRows) {
			return nil, apperrs.ErrNotFound
		}
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return &u, nil
}

// GetByUUID 根据 auth_user_uuid 查用户
func (r *AuthUserRepository) GetByUUID(ctx context.Context, authUserUUID string) (*model.AuthUser, error) {
	var u model.AuthUser
	err := r.db.GetContext(ctx, &u, `
		SELECT id, auth_user_uuid, username, email, password_hash, status, created_at, updated_at
		FROM auth_users WHERE auth_user_uuid = $1 LIMIT 1
	`, authUserUUID)
	if err != nil {
		if stderrs.Is(err, sql.ErrNoRows) {
			return nil, apperrs.ErrNotFound
		}
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return &u, nil
}

// GetByID 根据自增 ID 查用户
func (r *AuthUserRepository) GetByID(ctx context.Context, id int64) (*model.AuthUser, error) {
	var u model.AuthUser
	err := r.db.GetContext(ctx, &u, `
		SELECT id, auth_user_uuid, username, email, password_hash, status, created_at, updated_at
		FROM auth_users WHERE id = $1 LIMIT 1
	`, id)
	if err != nil {
		if stderrs.Is(err, sql.ErrNoRows) {
			return nil, apperrs.ErrNotFound
		}
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return &u, nil
}

// UpdateStatus 更新用户状态（active/disabled）
func (r *AuthUserRepository) UpdateStatus(ctx context.Context, id int64, status int16) error {
	res, err := r.db.ExecContext(ctx,
		"UPDATE auth_users SET status = $1, updated_at = NOW() WHERE id = $2",
		status, id,
	)
	if err != nil {
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	if n == 0 {
		return apperrs.ErrNotFound
	}
	return nil
}

// classifyUniqueViolation 从 Postgres 的约束名判断撞了哪个 UNIQUE
// Postgres 自动生成约束名格式：{table}_{column}_key
//
//	auth_users_username_key → username
//	auth_users_email_key    → email
//	auth_users_auth_user_uuid_key → uuid（正常注册不会撞）
//
// 零 SQL 查询，零竞态风险
func classifyUniqueViolation(constraintName string) error {
	switch {
	case strings.Contains(constraintName, "username"):
		return apperrs.ErrUsernameTaken
	case strings.Contains(constraintName, "email"):
		return apperrs.ErrEmailTaken
	case strings.Contains(constraintName, "auth_user_uuid"):
		// 概率极低（uuid v4 碰撞），视为已存在用户
		return apperrs.ErrUsernameTaken
	default:
		// 兜底：不知是哪个字段，但仍是重复
		return apperrs.ErrUsernameTaken
	}
}

// DB 暴露底层 sqlx.DB（供 AuthService 内部使用）
func (r *AuthUserRepository) DB() *sqlx.DB {
	return r.db
}

// 未使用变量占位（time.Now / fmt.Sprintf 在其他文件可能用到）
var _ = time.Now
var _ = fmt.Sprintf
