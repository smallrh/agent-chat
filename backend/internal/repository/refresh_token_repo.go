package repository

import (
	"context"
	"database/sql"
	stderrs "errors"
	"log/slog"
	"time"

	"github.com/jmoiron/sqlx"

	apperrs "agent/auth-service/internal/errors"
	"agent/auth-service/internal/model"
)

// RefreshTokenRepository Refresh Token 数据访问层
// token_hash 存 SHA256(plain) hex（128-bit 随机串 + SHA256 已足够）
type RefreshTokenRepository struct {
	db *sqlx.DB
}

// NewRefreshTokenRepository 构造
func NewRefreshTokenRepository(db *sqlx.DB) *RefreshTokenRepository {
	return &RefreshTokenRepository{db: db}
}

// Create 插入新 Refresh Token
func (r *RefreshTokenRepository) Create(ctx context.Context, token *model.RefreshToken) error {
	var parentID interface{}
	if token.ParentTokenID != nil {
		parentID = *token.ParentTokenID
	}

	query := `
		INSERT INTO refresh_tokens
			(auth_user_id, token_hash, parent_token_id, device_id, user_agent, expires_at, revoked_at)
		VALUES
			($1, $2, $3, $4, $5, $6, $7)
		RETURNING id, created_at
	`

	err := r.db.QueryRowContext(ctx, query,
		token.AuthUserID, token.TokenHash, parentID,
		token.DeviceID, token.UserAgent, token.ExpiresAt, token.RevokedAt,
	).Scan(&token.ID, &token.CreatedAt)

	if err != nil {
		slog.Error("refresh_tokens Create failed", "err", err)
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return nil
}

// GetByHash 根据 token_hash（SHA256 hex）查记录
func (r *RefreshTokenRepository) GetByHash(ctx context.Context, tokenHash string) (*model.RefreshToken, error) {
	var t model.RefreshToken
	err := r.db.GetContext(ctx, &t, `
		SELECT id, auth_user_id, token_hash, parent_token_id, device_id, user_agent,
		       expires_at, revoked_at, created_at
		FROM refresh_tokens WHERE token_hash = $1 LIMIT 1
	`, tokenHash)
	if err != nil {
		if stderrs.Is(err, sql.ErrNoRows) {
			return nil, apperrs.ErrInvalidToken
		}
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return &t, nil
}

// Revoke 撤销单个 Refresh Token（按记录 ID）
func (r *RefreshTokenRepository) Revoke(ctx context.Context, id int64) error {
	res, err := r.db.ExecContext(ctx,
		"UPDATE refresh_tokens SET revoked_at = NOW() WHERE id = $1 AND revoked_at IS NULL",
		id,
	)
	if err != nil {
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	if n == 0 {
		return nil // 已撤销或不存在，幂等
	}
	return nil
}

// RevokeAllForUser 撤销某用户所有 Refresh Token（Replay 检测后触发）
func (r *RefreshTokenRepository) RevokeAllForUser(ctx context.Context, authUserID int64) error {
	_, err := r.db.ExecContext(ctx,
		"UPDATE refresh_tokens SET revoked_at = NOW() WHERE auth_user_id = $1 AND revoked_at IS NULL",
		authUserID,
	)
	if err != nil {
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return nil
}

// Rotate 在同一个事务里执行 Refresh Token Rotation：
//  1. 撤销旧 token（revoked_at = NOW()）
//  2. 插入新 token，parent_token_id = 旧 token id
//
// 原子性保证：两步同成功或同失败，避免旧 token 被撤但新 token 没建成（用户被锁死）
func (r *RefreshTokenRepository) Rotate(ctx context.Context, oldTokenID int64, newToken *model.RefreshToken) error {
	tx, err := r.db.BeginTxx(ctx, nil)
	if err != nil {
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	defer func() { _ = tx.Rollback() }() // 兜底回滚

	// 步骤 1：撤销旧 token
	if _, err := tx.ExecContext(ctx,
		"UPDATE refresh_tokens SET revoked_at = NOW() WHERE id = $1 AND revoked_at IS NULL",
		oldTokenID,
	); err != nil {
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}

	// 步骤 2：插入新 token
	var parentID interface{}
	if newToken.ParentTokenID != nil {
		parentID = *newToken.ParentTokenID
	}

	err = tx.QueryRowContext(ctx, `
		INSERT INTO refresh_tokens
			(auth_user_id, token_hash, parent_token_id, device_id, user_agent, expires_at, revoked_at)
		VALUES
			($1, $2, $3, $4, $5, $6, $7)
		RETURNING id, created_at
	`,
		newToken.AuthUserID, newToken.TokenHash, parentID,
		newToken.DeviceID, newToken.UserAgent, newToken.ExpiresAt, newToken.RevokedAt,
	).Scan(&newToken.ID, &newToken.CreatedAt)
	if err != nil {
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}

	if err := tx.Commit(); err != nil {
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return nil
}

// DB 暴露底层 sqlx.DB（供 AuthService 内部使用）
func (r *RefreshTokenRepository) DB() *sqlx.DB {
	return r.db
}

// 确保 time 被使用（避免未使用 import）
var _ = time.Now
