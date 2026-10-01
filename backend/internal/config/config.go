package config

import (
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/joho/godotenv"
)

// Config 集中管理所有服务配置
type Config struct {
	AppEnv string
	Port   int

	// PostgreSQL
	PGHost     string
	PGPort     int
	PGDatabase string
	PGUser     string
	PGPassword string
	PGSSLMode  string

	// Redis
	RedisHost     string
	RedisPort     int
	RedisPassword string
	RedisDB       int

	// JWT
	JWTAlgorithm  string
	JWTIssuer     string
	JWTPrivateKey string // 路径
	JWTPublicKey  string // 路径
	AccessTTL     time.Duration
	RefreshTTL    time.Duration

	// Agent Service
	AgentServiceURL string
	AgentTimeout    time.Duration

	// Rate Limit
	LoginIPLimit     int
	LoginIPWindow    time.Duration
	LoginUserLimit   int
	LoginUserWindow  time.Duration
	RegisterIPLimit  int
	RegisterIPWindow time.Duration

	// 密钥自动生成目录
	KeysDir string
}

// Load 从 .env 加载配置，不存在时使用合理默认值
func Load() (*Config, error) {
	// 尝试加载 .env，不强制要求
	_ = godotenv.Load()

	cfg := &Config{
		AppEnv: getEnv("APP_ENV", "development"),
		Port:   getEnvInt("HTTP_PORT", 8080),

		PGHost:     getEnv("PG_HOST", "127.0.0.1"),
		PGPort:     getEnvInt("PG_PORT", 5432),
		PGDatabase: getEnv("PG_DATABASE", "agent_system"),
		PGUser:     getEnv("PG_USER", "postgres"),
		PGPassword: getEnv("PG_PASSWORD", "your_password"),
		PGSSLMode:  getEnv("PG_SSL_MODE", "disable"),

		RedisHost:     getEnv("REDIS_HOST", "127.0.0.1"),
		RedisPort:     getEnvInt("REDIS_PORT", 6379),
		RedisPassword: getEnv("REDIS_PASSWORD", ""),
		RedisDB:       getEnvInt("REDIS_DB", 0),

		JWTAlgorithm:  getEnv("JWT_ALGORITHM", "RS256"),
		JWTIssuer:     getEnv("JWT_ISSUER", "auth-service"),
		JWTPrivateKey: getEnv("JWT_PRIVATE_KEY_PATH", ""),
		JWTPublicKey:  getEnv("JWT_PUBLIC_KEY_PATH", ""),
		AccessTTL:     getEnvDuration("ACCESS_TOKEN_TTL", 15*time.Minute),
		RefreshTTL:    getEnvDuration("REFRESH_TOKEN_TTL", 7*24*time.Hour),

		AgentServiceURL: getEnv("AGENT_SERVICE_URL", "http://127.0.0.1:1111"),
		AgentTimeout:    getEnvDuration("AGENT_TIMEOUT", 30*time.Second),

		LoginIPLimit:     getEnvInt("LOGIN_IP_LIMIT", 5),
		LoginIPWindow:    getEnvDuration("LOGIN_IP_WINDOW", 1*time.Minute),
		LoginUserLimit:   getEnvInt("LOGIN_USER_LIMIT", 10),
		LoginUserWindow:  getEnvDuration("LOGIN_USER_WINDOW", 1*time.Minute),
		RegisterIPLimit:  getEnvInt("REGISTER_IP_LIMIT", 10),
		RegisterIPWindow: getEnvDuration("REGISTER_IP_WINDOW", 1*time.Minute),

		KeysDir: getEnv("KEYS_DIR", "./keys"),
	}

	return cfg, nil
}

// DSN 生成 PostgreSQL DSN
func (c *Config) DSN() string {
	return fmt.Sprintf(
		"host=%s port=%d user=%s password=%s dbname=%s sslmode=%s",
		c.PGHost, c.PGPort, c.PGUser, c.PGPassword, c.PGDatabase, c.PGSSLMode,
	)
}

// RedisAddr 返回 Redis 连接地址
func (c *Config) RedisAddr() string {
	return fmt.Sprintf("%s:%d", c.RedisHost, c.RedisPort)
}

// ------------------------------------------------------------
// 辅助函数
// ------------------------------------------------------------

func getEnv(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return fallback
}

func getEnvInt(key string, fallback int) int {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return fallback
}

func getEnvDuration(key string, fallback time.Duration) time.Duration {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return fallback
}
