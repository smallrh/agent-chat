package service

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	stderrs "errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"github.com/golang-jwt/jwt/v5"

	apperrs "agent/auth-service/internal/errors"

	"agent/auth-service/internal/config"
)

// TokenService JWT 签发 / 验证 / 密钥管理
type TokenService struct {
	cfg        *config.Config
	privateKey *rsa.PrivateKey
	publicKey  *rsa.PublicKey
	signingAlg string
}

// NewTokenService 加载（或自动生成）RSA 密钥对
func NewTokenService(cfg *config.Config) (*TokenService, error) {
	ts := &TokenService{
		cfg:        cfg,
		signingAlg: cfg.JWTAlgorithm,
	}

	privPath := cfg.JWTPrivateKey
	pubPath := cfg.JWTPublicKey

	if privPath == "" {
		privPath = filepath.Join(cfg.KeysDir, "jwt_private.pem")
	}
	if pubPath == "" {
		pubPath = filepath.Join(cfg.KeysDir, "jwt_public.pem")
	}

	privKey, pubKey, err := loadKeyPair(privPath, pubPath)
	if err != nil {
		slog.Info("JWT keys not found, auto-generating RSA-2048", "dir", cfg.KeysDir)
		if err := os.MkdirAll(cfg.KeysDir, 0o700); err != nil {
			return nil, fmt.Errorf("create keys dir: %w", err)
		}
		privKey, pubKey, err = generateKeyPair()
		if err != nil {
			return nil, fmt.Errorf("generate keys: %w", err)
		}
		if err := saveKeyPair(privKey, pubKey, privPath, pubPath); err != nil {
			return nil, fmt.Errorf("save keys: %w", err)
		}
	}

	ts.privateKey = privKey
	ts.publicKey = pubKey
	slog.Info("TokenService initialized", "algorithm", cfg.JWTAlgorithm)
	return ts, nil
}

// AccessTokenPayload Access Token 的 claims
type AccessTokenPayload struct {
	jwt.RegisteredClaims
	Type string `json:"type"`
}

// IssueAccess 签发 Access Token（sub 必须是 user_uuid）
func (ts *TokenService) IssueAccess(sub string) (string, time.Time, error) {
	now := time.Now()
	exp := now.Add(ts.cfg.AccessTTL)

	claims := AccessTokenPayload{
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   sub,
			Issuer:    ts.cfg.JWTIssuer,
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(exp),
			NotBefore: jwt.NewNumericDate(now),
		},
		Type: "access",
	}

	token := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	signed, err := token.SignedString(ts.privateKey)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("sign access token: %w", err)
	}
	return signed, exp, nil
}

// IssueRefresh 签发 Refresh Token 明文（随机串，明文只返回给客户端一次）
func (ts *TokenService) IssueRefresh() (plain string, hash string, expiresAt time.Time, err error) {
	buf := make([]byte, 64)
	if _, err = rand.Read(buf); err != nil {
		return "", "", time.Time{}, err
	}
	plain = fmt.Sprintf("%x", buf)
	expiresAt = time.Now().Add(ts.cfg.RefreshTTL)
	return plain, "", expiresAt, nil
}

// VerifyAccess 严格验证 Access Token
func (ts *TokenService) VerifyAccess(tokenString string) (*jwt.RegisteredClaims, error) {
	claims := &AccessTokenPayload{}

	token, err := jwt.ParseWithClaims(tokenString, claims, func(t *jwt.Token) (any, error) {
		if t.Method.Alg() != ts.signingAlg {
			return nil, fmt.Errorf("unexpected signing method: %s", t.Method.Alg())
		}
		return ts.publicKey, nil
	})
	if err != nil || !token.Valid {
		return nil, apperrs.Wrap(apperrs.ErrInvalidToken, err)
	}

	if claims.Issuer != ts.cfg.JWTIssuer {
		return nil, apperrs.ErrInvalidToken
	}

	if claims.Type != "access" {
		return nil, apperrs.ErrInvalidToken
	}

	return &claims.RegisteredClaims, nil
}

// ------------------------------------------------------------
// 密钥对管理
// ------------------------------------------------------------

func loadKeyPair(privPath, pubPath string) (*rsa.PrivateKey, *rsa.PublicKey, error) {
	privPEM, err := os.ReadFile(privPath)
	if err != nil {
		return nil, nil, err
	}
	pubPEM, err := os.ReadFile(pubPath)
	if err != nil {
		return nil, nil, err
	}

	block, _ := pem.Decode(privPEM)
	if block == nil {
		return nil, nil, stderrs.New("invalid private key PEM")
	}
	privKey, err := x509.ParsePKCS1PrivateKey(block.Bytes)
	if err != nil {
		pk8, err2 := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err2 != nil {
			return nil, nil, fmt.Errorf("parse private key: %w", err)
		}
		var ok bool
		privKey, ok = pk8.(*rsa.PrivateKey)
		if !ok {
			return nil, nil, stderrs.New("not an RSA private key")
		}
	}

	block, _ = pem.Decode(pubPEM)
	if block == nil {
		return nil, nil, stderrs.New("invalid public key PEM")
	}
	pubKey, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, nil, fmt.Errorf("parse public key: %w", err)
	}
	rsaPub, ok := pubKey.(*rsa.PublicKey)
	if !ok {
		return nil, nil, stderrs.New("not an RSA public key")
	}

	return privKey, rsaPub, nil
}

func generateKeyPair() (*rsa.PrivateKey, *rsa.PublicKey, error) {
	privKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		return nil, nil, err
	}
	return privKey, &privKey.PublicKey, nil
}

func saveKeyPair(privKey *rsa.PrivateKey, pubKey *rsa.PublicKey, privPath, pubPath string) error {
	privPEM := pem.EncodeToMemory(&pem.Block{
		Type:  "RSA PRIVATE KEY",
		Bytes: x509.MarshalPKCS1PrivateKey(privKey),
	})
	if err := os.WriteFile(privPath, privPEM, 0o600); err != nil {
		return err
	}

	pubBytes, err := x509.MarshalPKIXPublicKey(pubKey)
	if err != nil {
		return err
	}
	pubPEM := pem.EncodeToMemory(&pem.Block{
		Type:  "PUBLIC KEY",
		Bytes: pubBytes,
	})
	if err := os.WriteFile(pubPath, pubPEM, 0o644); err != nil {
		return err
	}

	slog.Info("JWT key pair saved", "private", privPath, "public", pubPath)
	return nil
}
