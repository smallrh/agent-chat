package service

import (
	"log/slog"

	"github.com/alexedwards/argon2id"
)

// PasswordService 密码哈希 / 验证服务（Argon2id）
type PasswordService struct {
	params *argon2id.Params
}

// NewPasswordService 使用默认 Argon2id 参数
func NewPasswordService() *PasswordService {
	return &PasswordService{
		params: &argon2id.Params{
			Memory:      64 * 1024, // 64MB
			Iterations:  3,
			Parallelism: 2,
			SaltLength:  16,
			KeyLength:   32,
		},
	}
}

// Hash 明文密码 → Argon2id 哈希
func (s *PasswordService) Hash(plaintext string) (string, error) {
	hash, err := argon2id.CreateHash(plaintext, s.params)
	if err != nil {
		slog.Error("Argon2id hash failed", "err", err)
		return "", err
	}
	return hash, nil
}

// Verify 明文密码 vs 存储的哈希
// 返回 true/false，不返回错误（内部差异直接视为不匹配）
func (s *PasswordService) Verify(plaintext, hash string) (bool, error) {
	match, err := argon2id.ComparePasswordAndHash(plaintext, hash)
	if err != nil {
		slog.Debug("Argon2id verify error (treating as mismatch)", "err", err)
		return false, nil
	}
	return match, nil
}
