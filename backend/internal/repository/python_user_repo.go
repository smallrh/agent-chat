package repository

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"

	"github.com/google/uuid"
	"github.com/jmoiron/sqlx"
)

// PythonUserRepository 负责往 Python Agent 的 users 表插入基础记录
// 确保 Python Agent 侧总能找到对应的 users.user_uuid
// Python Agent 零改动就能用
type PythonUserRepository struct {
	db *sqlx.DB
}

// NewPythonUserRepository 构造
func NewPythonUserRepository(db *sqlx.DB) *PythonUserRepository {
	return &PythonUserRepository{db: db}
}

// EnsureUser 确保 Python 的 users 表里存在对应 user_uuid 的记录
// 如果不存在则 INSERT（只写基础字段）
func (r *PythonUserRepository) EnsureUser(ctx context.Context, userUUID string) error {
	// 先查
	var count int
	err := r.db.GetContext(ctx, &count,
		"SELECT COUNT(*) FROM users WHERE user_uuid = $1", userUUID,
	)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			// 表不存在（可能还没初始化）
			slog.Warn("Python users table may not exist yet", "err", err)
			return nil
		}
		return err
	}

	if count > 0 {
		return nil // 已存在
	}

	// INSERT 基础记录
	newUUID, err := uuid.Parse(userUUID)
	if err != nil {
		return err
	}

	_, err = r.db.ExecContext(ctx, `
		INSERT INTO users (user_uuid, created_at, updated_at)
		VALUES ($1, NOW(), NOW())
	`, newUUID)
	if err != nil {
		// 可能并发插入导致唯一冲突，忽略
		var pgErr interface{ Code() string }
		if errors.As(err, &pgErr) && pgErr.Code() == "23505" {
			return nil
		}
		slog.Error("Python users INSERT failed", "err", err)
		return err
	}

	slog.Info("Ensured Python user record", "user_uuid", userUUID)
	return nil
}

// sql.ErrNoRows 已在顶部 import 声明
