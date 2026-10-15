package repository

import (
	"context"
	"database/sql"
	stderrs "errors"
	"log/slog"

	"github.com/jmoiron/sqlx"

	apperrs "agent/auth-service/internal/errors"

	"agent/auth-service/internal/config"
	"agent/auth-service/internal/model"
)

// UserPlanRepository 套餐/配额数据访问层（P3 计费）
//
// 约定：**查不到记录不是错误**，而是「该用户用 config 里的默认套餐」。
// 这样新用户注册后不需要写一行 user_plans，只有真正要覆盖时才插入。
type UserPlanRepository struct {
	db  *sqlx.DB
	cfg *config.Config
}

func NewUserPlanRepository(db *sqlx.DB, cfg *config.Config) *UserPlanRepository {
	return &UserPlanRepository{db: db, cfg: cfg}
}

// defaultPlan 兜底套餐（config 里的 QUOTA_*）
func (r *UserPlanRepository) defaultPlan(userUUID string) *model.UserPlan {
	return &model.UserPlan{
		UserUUID:      userUUID,
		PlanCode:      r.cfg.DefaultPlanCode,
		ChatPerDay:    r.cfg.QuotaChatPerDay,
		SearchPerDay:  r.cfg.QuotaSearchPerDay,
		TaskPerDay:    r.cfg.QuotaTaskPerDay,
	}
}

// GetByUUID 取某用户的套餐；无记录时返回默认套餐。
func (r *UserPlanRepository) GetByUUID(ctx context.Context, userUUID string) (*model.UserPlan, error) {
	if userUUID == "" {
		return nil, apperrs.ErrInvalidRequest
	}

	const query = `
		SELECT user_uuid, plan_code, chat_per_day, search_per_day, task_per_day, note, created_at, updated_at
		  FROM user_plans
		 WHERE user_uuid = $1
	`

	var p model.UserPlan
	err := r.db.GetContext(ctx, &p, query, userUUID)
	if err != nil {
		if stderrs.Is(err, sql.ErrNoRows) {
			return r.defaultPlan(userUUID), nil
		}
		slog.Error("user_plans GetByUUID failed, fallback to default plan",
			"user_uuid", userUUID, "err", err)
		// 查询失败（表没建 / PG 抖动）也回落默认套餐：
		// 计费系统挂了不能让用户聊不了天，最多是这一轮没拦住。
		return r.defaultPlan(userUUID), nil
	}
	return &p, nil
}

// Upsert 新增或更新某用户的套餐（管理后台用）
func (r *UserPlanRepository) Upsert(ctx context.Context, p *model.UserPlan) error {
	if p == nil || p.UserUUID == "" {
		return apperrs.ErrInvalidRequest
	}

	const query = `
		INSERT INTO user_plans
			(user_uuid, plan_code, chat_per_day, search_per_day, task_per_day, note)
		VALUES
			($1, $2, $3, $4, $5, $6)
		ON CONFLICT (user_uuid) DO UPDATE SET
			plan_code      = EXCLUDED.plan_code,
			chat_per_day   = EXCLUDED.chat_per_day,
			search_per_day = EXCLUDED.search_per_day,
			task_per_day   = EXCLUDED.task_per_day,
			note           = EXCLUDED.note,
			updated_at     = NOW()
		RETURNING created_at, updated_at
	`

	err := r.db.QueryRowContext(ctx, query,
		p.UserUUID, p.PlanCode, p.ChatPerDay, p.SearchPerDay, p.TaskPerDay, p.Note,
	).Scan(&p.CreatedAt, &p.UpdatedAt)
	if err != nil {
		slog.Error("user_plans Upsert failed", "user_uuid", p.UserUUID, "err", err)
		return apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return nil
}

// List 列出所有被覆盖过的套餐（管理后台看「谁被调过额度」）
func (r *UserPlanRepository) List(ctx context.Context, limit int) ([]model.UserPlan, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	const query = `
		SELECT user_uuid, plan_code, chat_per_day, search_per_day, task_per_day, note, created_at, updated_at
		  FROM user_plans
		 ORDER BY updated_at DESC
		 LIMIT $1
	`
	out := make([]model.UserPlan, 0, limit)
	if err := r.db.SelectContext(ctx, &out, query, limit); err != nil {
		slog.Error("user_plans List failed", "err", err)
		return nil, apperrs.Wrap(apperrs.ErrInternal, err)
	}
	return out, nil
}
