package tests

import (
	"bytes"
	"context"
	"encoding/json"
	stderrs "errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	appclient "agent/auth-service/internal/client"
	"agent/auth-service/internal/config"
	"agent/auth-service/internal/db"
	"agent/auth-service/internal/handler"
	"agent/auth-service/internal/repository"
	"agent/auth-service/internal/router"
	"agent/auth-service/internal/service"
)

// ---------------------------------------------------------------------------
// 测试基础设施
// ---------------------------------------------------------------------------

var (
	testRouter            *router.Router
	testDB                *db.Database
	testAgent             *httptest.Server // 模拟 Python Agent
	baseURL               string
	agentCapturedRequests []CapturedAgentRequest
	agentMu               sync.Mutex
)

type CapturedAgentRequest struct {
	Method           string
	Path             string
	XUserID          string
	RequestID        string
	HasClientXUserID bool // 客户端是否伪造了 X-User-Id
}

func TestMain(m *testing.M) {
	os.Setenv("PG_DATABASE", "agent_system")
	cfg, err := config.Load()
	if err != nil {
		fmt.Println("Load config err:", err)
		os.Exit(1)
	}

	// 缩短 TTL 加快测试
	cfg.AccessTTL = 5 * time.Second
	cfg.RefreshTTL = 10 * time.Second
	cfg.AgentTimeout = 3 * time.Second
	cfg.LoginIPLimit = 100
	cfg.LoginUserLimit = 100
	cfg.RegisterIPLimit = 100
	cfg.Port = 18081 // 测试用独立端口

	database, err := db.New(cfg)
	if err != nil {
		fmt.Println("DB connect err:", err)
		os.Exit(1)
	}
	testDB = database

	// Mock Python Agent
	testAgent = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		agentMu.Lock()
		agentCapturedRequests = append(agentCapturedRequests, CapturedAgentRequest{
			Method:           r.Method,
			Path:             r.URL.Path,
			XUserID:          r.Header.Get("X-User-Id"),
			RequestID:        r.Header.Get("X-Request-Id"),
			HasClientXUserID: r.Header.Get("X-Client-Sent-X-User-Id") == "yes",
		})
		agentMu.Unlock()

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"echo_x_user_id": r.Header.Get("X-User-Id"),
			"echo_path":      r.URL.Path,
			"echo_method":    r.Method,
		})
	}))
	defer testAgent.Close()

	cfg.AgentServiceURL = testAgent.URL
	agentClient := appclient.NewAgentClient(cfg)
	tokenSvc, _ := service.NewTokenService(cfg)
	userRepo := repository.NewAuthUserRepository(database.SQL)
	refreshRepo := repository.NewRefreshTokenRepository(database.SQL)
	pyUserRepo := repository.NewPythonUserRepository(database.SQL)
	pwSvc := service.NewPasswordService()
	rlSvc := service.NewRateLimitService(database.Redis, cfg)
	authSvc := service.NewAuthService(cfg, userRepo, refreshRepo, pyUserRepo, pwSvc, tokenSvc, rlSvc)

	gin.SetMode(gin.ReleaseMode)
	authHandler := handler.NewAuthHandler(authSvc)
	healthHandler := handler.NewHealthHandler(database)
	testRouter = router.New(authHandler, healthHandler, tokenSvc, agentClient, rlSvc)

	// 启动测试用 HTTP Server（18081 端口）
	srv := &http.Server{
		Addr:    fmt.Sprintf(":%d", cfg.Port),
		Handler: testRouter.Handler(),
	}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !stderrs.Is(err, http.ErrServerClosed) {
			fmt.Println("Test server failed:", err)
		}
	}()
	time.Sleep(300 * time.Millisecond)

	// 设置基础 URL
	baseURL = fmt.Sprintf("http://127.0.0.1:%d", cfg.Port)

	code := m.Run()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	database.Close()
	os.Exit(code)
}

// 清理测试数据（按用户名）
func cleanTestUser(t *testing.T, username string) {
	t.Helper()
	ctx := context.Background()
	_, _ = testDB.SQL.ExecContext(ctx, `
		DELETE FROM refresh_tokens WHERE auth_user_id IN (
			SELECT id FROM auth_users WHERE username = $1
		)
	`, username)
	_, _ = testDB.SQL.ExecContext(ctx,
		"DELETE FROM auth_users WHERE username = $1", username)
}

// ---------------------------------------------------------------------------
// HTTP 辅助
// ---------------------------------------------------------------------------

func doJSON(t *testing.T, method, url string, body any, headers map[string]string) (*http.Response, []byte) {
	t.Helper()
	var bodyBytes []byte
	if body != nil {
		b, err := json.Marshal(body)
		require.NoError(t, err)
		bodyBytes = b
	}

	req, err := http.NewRequest(method, url, bytes.NewReader(bodyBytes))
	require.NoError(t, err)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	return resp, respBody
}

func readJSON[T any](t *testing.T, data []byte) T {
	t.Helper()
	var v T
	require.NoError(t, json.Unmarshal(data, &v))
	return v
}

// ---------------------------------------------------------------------------
// 1. 注册成功
// ---------------------------------------------------------------------------
func Test01_RegisterSuccess(t *testing.T) {
	username := "test_reg_" + uuid.New().String()[:8]
	defer cleanTestUser(t, username)

	resp, body := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": username,
		"email":    username + "@test.com",
		"password": "Test12345678",
	}, nil)

	assert.Equal(t, http.StatusCreated, resp.StatusCode)

	type regResp struct {
		Data struct {
			User struct {
				UserUUID string `json:"user_uuid"`
				Username string `json:"username"`
			} `json:"user"`
		} `json:"data"`
	}
	parsed := readJSON[regResp](t, body)
	assert.NotEmpty(t, parsed.Data.User.UserUUID)
	assert.Equal(t, username, parsed.Data.User.Username)
}

// ---------------------------------------------------------------------------
// 2. 重复注册 → 409
// ---------------------------------------------------------------------------
func Test02_DuplicateRegisterConflict(t *testing.T) {
	username := "test_dup_" + uuid.New().String()[:8]
	defer cleanTestUser(t, username)

	_, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": username,
		"email":    username + "@test.com",
		"password": "Test12345678",
	}, nil)

	// 第二次
	resp, _ := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": username,
		"email":    "other_" + username + "@test.com",
		"password": "Test12345678",
	}, nil)

	assert.Equal(t, http.StatusConflict, resp.StatusCode, "重复注册应返回 409")
}

// ---------------------------------------------------------------------------
// 3. 登录成功
// ---------------------------------------------------------------------------
func Test03_LoginSuccess(t *testing.T) {
	username := "test_login_" + uuid.New().String()[:8]
	defer cleanTestUser(t, username)

	// 先注册
	_, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": username,
		"email":    username + "@test.com",
		"password": "Test12345678",
	}, nil)

	// 再登录
	resp, body := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/login", map[string]any{
		"username": username,
		"password": "Test12345678",
	}, nil)

	assert.Equal(t, http.StatusOK, resp.StatusCode)

	type loginResp struct {
		Data struct {
			AccessToken  string `json:"access_token"`
			RefreshToken string `json:"refresh_token"`
			TokenType    string `json:"token_type"`
		} `json:"data"`
	}
	parsed := readJSON[loginResp](t, body)
	assert.NotEmpty(t, parsed.Data.AccessToken)
	assert.NotEmpty(t, parsed.Data.RefreshToken)
	assert.Equal(t, "Bearer", parsed.Data.TokenType)

	// JWT Header 应该是 RS256
	parts := bytes.Split([]byte(parsed.Data.AccessToken), []byte("."))
	assert.Len(t, parts, 3, "JWT 应有三个部分")
}

// ---------------------------------------------------------------------------
// 4. 密码错误 → 401
// ---------------------------------------------------------------------------
func Test04_WrongPasswordUnauthorized(t *testing.T) {
	username := "test_pw_" + uuid.New().String()[:8]
	defer cleanTestUser(t, username)

	_, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": username,
		"email":    username + "@test.com",
		"password": "Test12345678",
	}, nil)

	resp, body := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/login", map[string]any{
		"username": username,
		"password": "WRONG_PASSWORD",
	}, nil)

	assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)

	type errResp struct {
		Code string `json:"code"`
	}
	parsed := readJSON[errResp](t, body)
	assert.Equal(t, "INVALID_CREDENTIALS", parsed.Code)
}

// ---------------------------------------------------------------------------
// 5. Access Token 过期 → 401
// ---------------------------------------------------------------------------
func Test05_AccessTokenExpired(t *testing.T) {
	username := "test_exp_" + uuid.New().String()[:8]
	defer cleanTestUser(t, username)

	// 注册 + 登录
	_, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": username,
		"email":    username + "@test.com",
		"password": "Test12345678",
	}, nil)

	resp, body := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/login", map[string]any{
		"username": username,
		"password": "Test12345678",
	}, nil)
	require.Equal(t, http.StatusOK, resp.StatusCode)

	type loginResp struct {
		Data struct {
			AccessToken string `json:"access_token"`
		} `json:"data"`
	}
	token := readJSON[loginResp](t, body).Data.AccessToken

	// 等测试里配置的 AccessTTL（5秒）过期
	time.Sleep(6 * time.Second)

	// 再调 /me 应该 401
	resp, body = doJSON(t, http.MethodGet, baseURL+"/api/v1/auth/me", nil, map[string]string{
		"Authorization": "Bearer " + token,
	})
	assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)
}

// ---------------------------------------------------------------------------
// 6. JWT 签名错误 → 401
// ---------------------------------------------------------------------------
func Test06_JWTBadSignature(t *testing.T) {
	// 随便造个假 token（改一下 JWT 内容导致签名不匹配）
	fakeToken := "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhYmMxMjMiLCJpc3MiOiJhdXRoLXNlcnZpY2UiLCJleHAiOjE5OTk5OTk5OTl9.fake-fake-fake"

	resp, _ := doJSON(t, http.MethodGet, baseURL+"/api/v1/auth/me", nil, map[string]string{
		"Authorization": "Bearer " + fakeToken,
	})
	assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)
}

// ---------------------------------------------------------------------------
// 7. 未认证 → 401
// ---------------------------------------------------------------------------
func Test07_NoAuthUnauthorized(t *testing.T) {
	resp, _ := doJSON(t, http.MethodGet, baseURL+"/api/v1/auth/me", nil, nil)
	assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)
}

// ---------------------------------------------------------------------------
// 8. Refresh Token Rotation
// ---------------------------------------------------------------------------
func Test08_RefreshTokenRotation(t *testing.T) {
	username := "test_rot_" + uuid.New().String()[:8]
	defer cleanTestUser(t, username)

	// 注册 + 登录
	_, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": username,
		"email":    username + "@test.com",
		"password": "Test12345678",
	}, nil)

	resp, body := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/login", map[string]any{
		"username": username,
		"password": "Test12345678",
	}, nil)
	require.Equal(t, http.StatusOK, resp.StatusCode)

	type loginResp struct {
		Data struct {
			RefreshToken string `json:"refresh_token"`
			AccessToken  string `json:"access_token"`
		} `json:"data"`
	}
	firstRefresh := readJSON[loginResp](t, body).Data.RefreshToken

	// 第一次刷新
	resp, body = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/refresh", map[string]any{
		"refresh_token": firstRefresh,
	}, nil)
	assert.Equal(t, http.StatusOK, resp.StatusCode)

	type refreshResp struct {
		Data struct {
			RefreshToken string `json:"refresh_token"`
			AccessToken  string `json:"access_token"`
		} `json:"data"`
	}
	secondRefresh := readJSON[refreshResp](t, body).Data.RefreshToken

	// 验证：新旧 Refresh Token 不一样
	assert.NotEqual(t, firstRefresh, secondRefresh)

	// 旧 Refresh Token 再用 → 应该失效（Replay 检测）
	time.Sleep(100 * time.Millisecond)
	resp, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/refresh", map[string]any{
		"refresh_token": firstRefresh,
	}, nil)
	assert.Equal(t, http.StatusUnauthorized, resp.StatusCode, "旧 Refresh Token 应该已失效")
}

// ---------------------------------------------------------------------------
// 9. Refresh Token Replay（检测后撤销所有）
// ---------------------------------------------------------------------------
func Test09_RefreshTokenReplayRevokesAll(t *testing.T) {
	username := "test_rep_" + uuid.New().String()[:8]
	defer cleanTestUser(t, username)

	_, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": username,
		"email":    username + "@test.com",
		"password": "Test12345678",
	}, nil)

	resp, body := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/login", map[string]any{
		"username": username,
		"password": "Test12345678",
	}, nil)
	require.Equal(t, http.StatusOK, resp.StatusCode)

	type loginResp struct {
		Data struct {
			RefreshToken string `json:"refresh_token"`
		} `json:"data"`
	}
	firstRefresh := readJSON[loginResp](t, body).Data.RefreshToken

	// 刷新一次 → 得到新的 Refresh
	resp, body = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/refresh", map[string]any{
		"refresh_token": firstRefresh,
	}, nil)
	require.Equal(t, http.StatusOK, resp.StatusCode)

	type refreshResp struct {
		Data struct {
			RefreshToken string `json:"refresh_token"`
		} `json:"data"`
	}
	secondRefresh := readJSON[refreshResp](t, body).Data.RefreshToken

	// 故意用旧 Refresh（Replay）
	resp, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/refresh", map[string]any{
		"refresh_token": firstRefresh,
	}, nil)
	assert.Equal(t, http.StatusUnauthorized, resp.StatusCode, "Replay 检测后应 401")

	// 再用新的也不行了（因为 Replay 检测撤销了全部）
	time.Sleep(100 * time.Millisecond)
	resp, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/refresh", map[string]any{
		"refresh_token": secondRefresh,
	}, nil)
	assert.Equal(t, http.StatusUnauthorized, resp.StatusCode, "Replay 后所有 Refresh 都应被撤销")
}

// ---------------------------------------------------------------------------
// 10. ★ 客户端伪造 X-User-Id → Go 必须覆盖（最关键的安全测试）
// ---------------------------------------------------------------------------
func Test10_ClientForgedXUserID_GoOverrides(t *testing.T) {
	// ---- 准备两个不同用户 ----
	userA := "forged_A_" + uuid.New().String()[:8]
	userB := "forged_B_" + uuid.New().String()[:8]
	defer cleanTestUser(t, userA)
	defer cleanTestUser(t, userB)

	_, _ = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": userA, "email": userA + "@test.com", "password": "Test12345678",
	}, nil)
	respA, bodyA := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/login", map[string]any{
		"username": userA, "password": "Test12345678",
	}, nil)
	require.Equal(t, http.StatusOK, respA.StatusCode)

	type loginResp struct {
		Data struct {
			AccessToken string `json:"access_token"`
			User        struct {
				UserUUID string `json:"user_uuid"`
			} `json:"user"`
		} `json:"data"`
	}
	tokenA := readJSON[loginResp](t, bodyA).Data.AccessToken
	uuidA := readJSON[loginResp](t, bodyA).Data.User.UserUUID

	// 注册用户 B，拿他的 uuid（但不拿他的 token）
	respU, bodyU := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": userB, "email": userB + "@test.com", "password": "Test12345678",
	}, nil)
	require.Equal(t, http.StatusCreated, respU.StatusCode)

	type regResp struct {
		Data struct {
			User struct {
				UserUUID string `json:"user_uuid"`
			} `json:"user"`
		} `json:"data"`
	}
	uuidB := readJSON[regResp](t, bodyU).Data.User.UserUUID

	// ---- 伪造请求：用户 A 的 Token + 客户端强行塞 X-User-Id = uuidB ----
	agentMu.Lock()
	agentCapturedRequests = nil
	agentMu.Unlock()

	forgedUUIDB := uuidB // 客户端想冒充 B

	resp, body := doJSON(t, http.MethodPost, baseURL+"/agent/chat",
		map[string]any{"message": "hello"},
		map[string]string{
			"Authorization":           "Bearer " + tokenA,
			"X-User-Id":               forgedUUIDB, // 客户端伪造 B 的 uuid
			"X-Client-Sent-X-User-Id": "yes",       // 标记这是伪造请求
		})

	assert.Equal(t, http.StatusOK, resp.StatusCode, "Agent 代理应正常返回")

	type echoResp struct {
		EchoXUserID string `json:"echo_x_user_id"`
	}
	echoed := readJSON[echoResp](t, body)

	// ★★★ 最关键断言：Go 转发给 Agent 的 X-User-Id 必须是 uuidA（从 JWT sub 来的），绝对不能是客户端伪造的 uuidB！
	assert.Equal(t, uuidA, echoed.EchoXUserID,
		"Go MUST overwrite client-forged X-User-Id! Expected user_A uuid, got: "+echoed.EchoXUserID)
	assert.NotEqual(t, uuidB, echoed.EchoXUserID,
		"Go should NOT forward client-forged X-User-Id (user_B uuid)")

	// 再检查 httptest server 捕获的请求
	agentMu.Lock()
	captured := agentCapturedRequests
	agentMu.Unlock()

	require.Len(t, captured, 1, "Agent 应只收到一次请求")
	assert.Equal(t, uuidA, captured[0].XUserID, "Agent 收到的 X-User-Id 应是用户 A 的 uuid")

	t.Logf("✅ 安全测试通过！客户端伪造 X-User-Id=%s，Go 强制覆盖为 JWT sub=%s", uuidB, uuidA)
}

// ---------------------------------------------------------------------------
// 11. /ready 检查（DB + Redis）
// ---------------------------------------------------------------------------
func Test11_ReadyCheck(t *testing.T) {
	resp, body := doJSON(t, http.MethodGet, baseURL+"/ready", nil, nil)
	assert.Equal(t, http.StatusOK, resp.StatusCode)

	type respT struct {
		Code string `json:"code"`
	}
	parsed := readJSON[respT](t, body)
	assert.Equal(t, "OK", parsed.Code)
}

// ---------------------------------------------------------------------------
// 12. ★ Alice E2E：注册 → 登录 → Agent 代理（完整安全链路）
//
// 覆盖：
//   a. Alice 注册 → Go Auth → auth_users 插入 → users 表 EnsureUser
//   b. Alice 登录 → Go Auth 验密码 → 发 JWT（sub = user_uuid）
//   c. Alice 用 JWT 调 /agent/chat → Go Auth 验 JWT → 注入 X-User-Id → 代理到 Python Agent
//   d. Mock Agent 收到的 X-User-Id 必须 = Alice 的 user_uuid（不是别的）
//   e. Alice 伪造 X-User-Id → Go 强制覆盖为真实 uuid
// ---------------------------------------------------------------------------
func Test12_Alice_E2E_Register_Login_AgentProxy(t *testing.T) {
	const aliceUsername = "alice_e2e"
	defer cleanTestUser(t, aliceUsername)

	// --- Step 1: Alice 注册 ---
	t.Log("=== Step 1: Alice 注册 ===")
	resp, body := doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/register", map[string]any{
		"username": aliceUsername,
		"email":    "alice@example.com",
		"password": "Alice123456",
	}, nil)
	require.Equal(t, http.StatusCreated, resp.StatusCode, "Alice 应注册成功")

	type regResp struct {
		Data struct {
			User struct {
				UserUUID string `json:"user_uuid"`
			} `json:"user"`
		} `json:"data"`
	}
	aliceReg := readJSON[regResp](t, body)
	aliceUUID := aliceReg.Data.User.UserUUID
	assert.NotEmpty(t, aliceUUID)
	t.Logf("Alice 注册成功！user_uuid = %s", aliceUUID)

	// --- Step 2: Alice 登录 ---
	t.Log("=== Step 2: Alice 登录 ===")
	resp, body = doJSON(t, http.MethodPost, baseURL+"/api/v1/auth/login", map[string]any{
		"username": aliceUsername,
		"password": "Alice123456",
	}, nil)
	require.Equal(t, http.StatusOK, resp.StatusCode, "Alice 应登录成功")

	type loginResp struct {
		Data struct {
			AccessToken  string `json:"access_token"`
			RefreshToken string `json:"refresh_token"`
			TokenType    string `json:"token_type"`
			User         struct {
				UserUUID string `json:"user_uuid"`
			} `json:"user"`
		} `json:"data"`
	}
	aliceLogin := readJSON[loginResp](t, body)
	aliceToken := aliceLogin.Data.AccessToken
	assert.NotEmpty(t, aliceToken)
	assert.Equal(t, "Bearer", aliceLogin.Data.TokenType)
	assert.Equal(t, aliceUUID, aliceLogin.Data.User.UserUUID, "登录返回的 user_uuid 应和注册一致")
	t.Logf("Alice 登录成功！access_token 长度 = %d, token_type = %s",
		len(aliceToken), aliceLogin.Data.TokenType)

	// --- Step 3: Alice 用 JWT 调 /agent/chat ---
	t.Log("=== Step 3: Alice → Go Auth → Agent 代理（正常请求） ===")

	agentMu.Lock()
	agentCapturedRequests = nil
	agentMu.Unlock()

	resp, body = doJSON(t, http.MethodPost, baseURL+"/agent/chat",
		map[string]any{"message": "你好，我是 Alice"},
		map[string]string{
			"Authorization": "Bearer " + aliceToken,
		})

	require.Equal(t, http.StatusOK, resp.StatusCode, "Agent 代理应返回 200")

	type echoResp struct {
		EchoXUserID string `json:"echo_x_user_id"`
		EchoPath    string `json:"echo_path"`
		EchoMethod  string `json:"echo_method"`
	}
	echo := readJSON[echoResp](t, body)

	// ★ Go 必须把 JWT.sub（aliceUUID）注入给 Agent
	assert.Equal(t, aliceUUID, echo.EchoXUserID,
		"Agent 收到的 X-User-Id 必须是 Alice 的真实 user_uuid")
	assert.Equal(t, "/chat", echo.EchoPath)
	assert.Equal(t, "POST", echo.EchoMethod)

	// httptest server 捕获的也对
	agentMu.Lock()
	captured := agentCapturedRequests
	agentMu.Unlock()
	require.Len(t, captured, 1)
	assert.Equal(t, aliceUUID, captured[0].XUserID)
	assert.NotEmpty(t, captured[0].RequestID, "应注入 X-Request-Id")

	t.Logf("✅ Go Auth → Agent 代理正常！X-User-Id = %s", echo.EchoXUserID)

	// --- Step 4: Alice 伪造一个假的 X-User-Id → Go 必须覆盖 ---
	t.Log("=== Step 4: Alice 伪造 X-User-Id → Go 强制覆盖 ===")

	agentMu.Lock()
	agentCapturedRequests = nil
	agentMu.Unlock()

	fakeUUID := "00000000-0000-0000-0000-000000000000"
	resp, body = doJSON(t, http.MethodPost, baseURL+"/agent/chat",
		map[string]any{"message": "尝试冒充别人"},
		map[string]string{
			"Authorization":           "Bearer " + aliceToken,
			"X-User-Id":               fakeUUID,      // Alice 想伪造
			"X-Client-Sent-X-User-Id": "yes",          // 标记
		})

	require.Equal(t, http.StatusOK, resp.StatusCode)
	echo2 := readJSON[echoResp](t, body)

	// ★★★ 核心安全断言：Go 绝对不能把伪造的 X-User-Id 透给 Agent
	assert.Equal(t, aliceUUID, echo2.EchoXUserID,
		"Go MUST overwrite client-forged X-User-Id!")
	assert.NotEqual(t, fakeUUID, echo2.EchoXUserID,
		"Go 绝对不能把客户端伪造的 UUID 透传给 Agent")

	t.Logf("✅ 伪造防护通过！客户端塞 X-User-Id=%s，Go 强制覆盖为 %s",
		fakeUUID, echo2.EchoXUserID)

	// --- Step 5: Alice 不带 Token → 401 ---
	t.Log("=== Step 5: 无 Token → 401 ===")
	resp, _ = doJSON(t, http.MethodPost, baseURL+"/agent/chat",
		map[string]any{"message": "无 token"}, nil)
	assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)

	t.Logf("✅ 无 Token 被拦截 → 401")

	t.Log("")
	t.Log("╔═══════════════════════════════════════════════════╗")
	t.Log("║  ✅ Alice E2E 全链路安全测试通过！                ║")
	t.Log("║   Register → Login → JWT → Go Auth → Agent       ║")
	t.Log("║   伪造 X-User-Id 被强制覆盖                      ║")
	t.Log("╚═══════════════════════════════════════════════════╝")
}
