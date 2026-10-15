package service

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"

	apperrs "agent/auth-service/internal/errors"
	"agent/auth-service/internal/model"
	"agent/auth-service/internal/repository"
)

// 计量维度。**必须与 Agent 侧 app/service/usage_service.py 完全一致**，
// 否则 Go 读到的计数永远是 0（表现为「用了很多但配额没动」）。
const (
	MetricChat   = "chat"
	MetricSearch = "web_search"
	MetricTask   = "task"
)

// AllMetrics 顺序即前端展示顺序
var AllMetrics = []string{MetricChat, MetricSearch, MetricTask}

// QuotaService 配额判定 + 用量查询（P3 计费）
//
// 职责边界：
//   - 计数由 Agent 侧写（每轮对话结束后 incr），这里只**读**
//   - 拦截在请求进入 Agent 之前（见 middleware/quota.go）
//
// 故障策略：**Redis 或 PG 挂了一律放行**。
// 理由与限流一致 —— 计费系统故障导致用户聊不了天，比「这一轮没拦住」
// 严重得多。真要对账，后面可以再补 PG 准实时落库。
type QuotaService struct {
	rdb     *redis.Client
	planRepo *repository.UserPlanRepository
	cfg     *configLike
}

// configLike 只取 QuotaService 真正需要的字段，
// 避免 service 层直接依赖整个 config 结构体（便于测试时替换）。
type configLike struct {
	QuotaEnabled bool
}

func NewQuotaService(rdb *redis.Client, planRepo *repository.UserPlanRepository, quotaEnabled bool) *QuotaService {
	return &QuotaService{
		rdb:      rdb,
		planRepo: planRepo,
		cfg:      &configLike{QuotaEnabled: quotaEnabled},
	}
}

// ------------------------------------------------------------
// Key（必须与 Agent 侧 usage_service.py 的 day_key / month_key 一致）
// ------------------------------------------------------------

func dayKey(metric, userUUID string) string {
	return fmt.Sprintf("usage:%s:%s:%s", metric, userUUID, time.Now().UTC().Format("20060102"))
}

func monthKey(metric, userUUID string) string {
	return fmt.Sprintf("usage:m:%s:%s:%s", metric, userUUID, time.Now().UTC().Format("200601"))
}

func readInt(ctx context.Context, rdb *redis.Client, key string) int {
	v, err := rdb.Get(ctx, key).Int()
	if err != nil {
		// redis.Nil（key 不存在）也是错误，这里统一当 0，不刷日志
		return 0
	}
	return v
}

// ------------------------------------------------------------
// 用量查询
// ------------------------------------------------------------

// Usage 取某用户的完整用量快照（今日 / 本月 + 配额）
func (s *QuotaService) Usage(ctx context.Context, userUUID string) (*model.UsageResponse, error) {
	plan, err := s.planRepo.GetByUUID(ctx, userUUID)
	if err != nil {
		return nil, err
	}

	items := make(map[string]model.QuotaItem, len(AllMetrics))
	for _, m := range AllMetrics {
		used := readInt(ctx, s.rdb, dayKey(m, userUUID))
		month := readInt(ctx, s.rdb, monthKey(m, userUUID))
		limit := s.limitFor(plan, m)

		remain := -1 // -1 = 不限
		if limit > 0 {
			remain = limit - used
			if remain < 0 {
				remain = 0
			}
		}
		items[m] = model.QuotaItem{Used: used, Limit: limit, Month: month, Remain: remain}
	}

	return &model.UsageResponse{
		UserUUID: userUUID,
		PlanCode: plan.PlanCode,
		Day:      time.Now().UTC().Format("20060102"),
		Month:    time.Now().UTC().Format("200601"),
		Items:    items,
	}, nil
}

func (s *QuotaService) limitFor(p *model.UserPlan, metric string) int {
	switch metric {
	case MetricChat:
		return p.ChatPerDay
	case MetricSearch:
		return p.SearchPerDay
	case MetricTask:
		return p.TaskPerDay
	}
	return 0
}

// ------------------------------------------------------------
// 配额判定
// ------------------------------------------------------------

// CheckMetric 判定「该用户今天还能不能再做一次 metric」。
// 返回 nil = 放行；否则返回 ErrQuotaExceeded。
func (s *QuotaService) CheckMetric(ctx context.Context, userUUID, metric string) error {
	if !s.cfg.QuotaEnabled {
		return nil
	}
	if userUUID == "" {
		return nil // 没有身份信息时不拦（鉴权中间件会先兜住）
	}

	plan, err := s.planRepo.GetByUUID(ctx, userUUID)
	if err != nil {
		slog.Warn("quota: plan lookup failed, allow", "user_uuid", userUUID, "err", err)
		return nil
	}

	limit := s.limitFor(plan, metric)
	if limit <= 0 {
		return nil // 0 = 不限
	}

	used := readInt(ctx, s.rdb, dayKey(metric, userUUID))
	if used >= limit {
		slog.Info("quota exceeded", "user_uuid", userUUID,
			"metric", metric, "used", used, "limit", limit)
		return apperrs.ErrQuotaExceeded
	}
	return nil
}
