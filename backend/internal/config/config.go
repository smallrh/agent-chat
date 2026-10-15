package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
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
	ChatUserLimit    int
	ChatUserWindow   time.Duration
	SSETimeout       time.Duration

	// 单用户并发 SSE（/chat/stream）上限：0 = 不限
	ChatStreamConcurrency int
	// 并发计数的兜底 TTL：进程被 kill 时计数可能没归还，靠 TTL 自动清零
	ChatStreamTTL time.Duration

	// HTTP Server 写超时。
	// ★ 必须 >= SSETimeout：Go 的 WriteTimeout 从「请求读完」起算，覆盖整个响应写过程，
	//   SSE 长生成会被它硬切。Agent 侧配置的 AGENT_SSE_TIMEOUT 才有意义。
	ServerWriteTimeout time.Duration

	// 密钥自动生成目录
	KeysDir string

	// ------------------------------------------------------------
	// P3 计费 / 配额
	// ------------------------------------------------------------
	// 总开关。关掉后 QuotaMiddleware 直接放行（排障 / 内测时用）。
	QuotaEnabled bool
	// 默认套餐：user_plans 表里没有记录的用户使用这一组额度。
	// 配额按「天」统计，0 = 不限。
	DefaultPlanCode   string
	QuotaChatPerDay   int
	QuotaSearchPerDay int
	QuotaTaskPerDay   int

	// 管理后台 API 的共享令牌（请求头 X-Admin-Token）。
	// 留空 = 管理端点整体禁用（生产不配就访问不了，安全默认值）。
	// 用独立令牌而不是引入角色体系：管理后台是低频内部操作，
	// 为它给 auth_users 加 role 列 + 改 JWT claims 会牵动整条登录链路。
	AdminToken string
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
		SSETimeout:      getEnvDuration("AGENT_SSE_TIMEOUT", 10*time.Minute),

		LoginIPLimit:     getEnvInt("LOGIN_IP_LIMIT", 5),
		LoginIPWindow:    getEnvDuration("LOGIN_IP_WINDOW", 1*time.Minute),
		LoginUserLimit:   getEnvInt("LOGIN_USER_LIMIT", 10),
		LoginUserWindow:  getEnvDuration("LOGIN_USER_WINDOW", 1*time.Minute),
		RegisterIPLimit:  getEnvInt("REGISTER_IP_LIMIT", 10),
		RegisterIPWindow: getEnvDuration("REGISTER_IP_WINDOW", 1*time.Minute),
		ChatUserLimit:    getEnvInt("CHAT_USER_LIMIT", 30),
		ChatUserWindow:   getEnvDuration("CHAT_USER_WINDOW", 1*time.Minute),

		// 单用户同时最多 2 条流式对话（多开标签页/多点几下就会触顶）
		ChatStreamConcurrency: getEnvInt("CHAT_STREAM_CONCURRENCY", 2),
		ChatStreamTTL:         getEnvDuration("CHAT_STREAM_TTL", 11*time.Minute),

		// 默认 15 分钟：为 SSE 让路（AGENT_SSE_TIMEOUT 默认 10 分钟）
		ServerWriteTimeout: getEnvDuration("HTTP_WRITE_TIMEOUT", 15*time.Minute),

		KeysDir: getEnv("KEYS_DIR", "./keys"),

		// ---- P3 计费 / 配额 ----
		QuotaEnabled:      getEnvBool("QUOTA_ENABLED", true),
		DefaultPlanCode:   getEnv("DEFAULT_PLAN_CODE", "free"),
		// 默认值按「个人正常使用」给：一天 200 轮对话足够闲聊 + 若干次调研，
		// 50 次联网检索是成本大头（外部 API 按次计费），10 个后台任务防脚本滥用。
		QuotaChatPerDay:   getEnvInt("QUOTA_CHAT_PER_DAY", 200),
		QuotaSearchPerDay: getEnvInt("QUOTA_SEARCH_PER_DAY", 50),
		QuotaTaskPerDay:   getEnvInt("QUOTA_TASK_PER_DAY", 10),

		// 留空 = 管理端点禁用。生产环境必须显式配置。
		AdminToken: getEnv("ADMIN_TOKEN", ""),
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

// getEnvBool 解析布尔配置。接受 1/0、true/false、yes/no、on/off。
// 注意：只要显式配置了值但解析不出来，就回落默认值而不是当成 true ——
// 把 "QUOTA_ENABLED=ture"（拼错）当成 true 会让一个本该关掉的开关悄悄打开。
func getEnvBool(key string, fallback bool) bool {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		return fallback
	}
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}
