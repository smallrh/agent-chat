package model

import "time"

// UserPlan 用户套餐 / 配额（P3 计费）
//
// 约定：0 = 不限。所以判断「是否超额」必须先判 limit > 0，
// 写成 `used >= limit` 而不判 0，会把「不限」的用户在第 0 次就拦下来。
type UserPlan struct {
	UserUUID     string    `db:"user_uuid" json:"user_uuid"`
	PlanCode     string    `db:"plan_code" json:"plan_code"`
	ChatPerDay   int       `db:"chat_per_day" json:"chat_per_day"`
	SearchPerDay int       `db:"search_per_day" json:"search_per_day"`
	TaskPerDay   int       `db:"task_per_day" json:"task_per_day"`
	Note         string    `db:"note" json:"note,omitempty"`
	CreatedAt    time.Time `db:"created_at" json:"created_at,omitempty"`
	UpdatedAt    time.Time `db:"updated_at" json:"updated_at,omitempty"`
}

// QuotaItem 单项用量 + 配额
type QuotaItem struct {
	Used   int `json:"used"`             // 今日已用
	Limit  int `json:"limit"`            // 每日额度；0 = 不限
	Month  int `json:"month"`            // 本月累计（仅展示/对账，不做拦截）
	Remain int `json:"remain"`           // 今日剩余；不限时为 -1
}

// UsageResponse /api/v1/usage/me 的响应
type UsageResponse struct {
	UserUUID string                `json:"user_uuid"`
	PlanCode string                `json:"plan_code"`
	Day      string                `json:"day"`   // YYYYMMDD
	Month    string                `json:"month"` // YYYYMM
	Items    map[string]QuotaItem  `json:"items"` // chat / web_search / task
}

// AdminUsageResponse 管理后台：查某个用户的用量
type AdminUsageResponse struct {
	UsageResponse
	Username string `json:"username,omitempty"`
	Email    string `json:"email,omitempty"`
	Status   int16  `json:"status,omitempty"` // auth_users.status: 1=正常 2=已封禁
}
