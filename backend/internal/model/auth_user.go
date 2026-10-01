package model

import "time"

// AuthUser 认证用户表（独立于 Python Agent 的 users 表）
type AuthUser struct {
	ID           int64     `db:"id" json:"-"`
	AuthUserUUID string    `db:"auth_user_uuid" json:"user_uuid"`
	Username     string    `db:"username" json:"username"`
	Email        string    `db:"email" json:"email"`
	PasswordHash string    `db:"password_hash" json:"-"` // 永远不返回
	Status       int16     `db:"status" json:"-"`        // 1=active, 2=disabled
	CreatedAt    time.Time `db:"created_at" json:"created_at"`
	UpdatedAt    time.Time `db:"updated_at" json:"updated_at"`
}

// IsActive 状态判断
func (u *AuthUser) IsActive() bool {
	return u.Status == 1
}

// AuthUserPublic 对外返回的用户结构（不含敏感字段）
type AuthUserPublic struct {
	UserUUID string `json:"user_uuid"`
	Username string `json:"username"`
	Email    string `json:"email"`
}

// ToPublic 转换为安全的对外结构
func (u *AuthUser) ToPublic() AuthUserPublic {
	return AuthUserPublic{
		UserUUID: u.AuthUserUUID,
		Username: u.Username,
		Email:    u.Email,
	}
}

// RefreshToken 刷新令牌记录
// token_hash = SHA256(plain) hex（128-bit 随机串 preimage 完全不可能，SHA256 已足够，
// 不需要 argon2id 这种为人类密码设计的慢速哈希）
type RefreshToken struct {
	ID            int64      `db:"id" json:"-"`
	AuthUserID    int64      `db:"auth_user_id" json:"-"`
	TokenHash     string     `db:"token_hash" json:"-"`
	ParentTokenID *int64     `db:"parent_token_id" json:"-"`
	DeviceID      string     `db:"device_id" json:"-"`
	UserAgent     string     `db:"user_agent" json:"-"`
	ExpiresAt     time.Time  `db:"expires_at" json:"-"`
	RevokedAt     *time.Time `db:"revoked_at" json:"-"`
	CreatedAt     time.Time  `db:"created_at" json:"-"`
}

// IsRevoked 是否已撤销
func (t *RefreshToken) IsRevoked() bool {
	return t.RevokedAt != nil
}

// IsExpired 是否已过期
func (t *RefreshToken) IsExpired() bool {
	return t.ExpiresAt.Before(time.Now())
}
