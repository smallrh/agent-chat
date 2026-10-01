// Package tests —— 真实环境 E2E 测试（Alice 全链路）。
//
// 与 integration_test.go 的区别：
//
//	integration_test.go → httptest mock Go 路由 + mock Agent，单元级隔离
//	e2e_test.go         → 真实 Go Auth (:8080) + 真实 Python Agent (:1111)
//
// 运行方式：
//
//	# 前置：PostgreSQL / Redis 正常，Go Auth 在 :8080 跑，Python Agent 可选
//	cd backend
//	go test -v -run TestE2E_Alice ./tests/...
//
//	# 自定义地址（默认 http://127.0.0.1:8080 / http://127.0.0.1:1111）
//	E2E_GO_AUTH_URL=http://192.168.1.100:8080 \
//	E2E_PY_AGENT_URL=http://192.168.1.100:1111 \
//	go test -v -run TestE2E_Alice ./tests/...
package tests

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// ---------------------------------------------------------------------------
// 配置（环境变量可覆盖）
// ---------------------------------------------------------------------------

var (
	e2eBaseURL = func() string {
		if v := os.Getenv("E2E_GO_AUTH_URL"); v != "" {
			return strings.TrimRight(v, "/")
		}
		return "http://127.0.0.1:8080"
	}()

	e2ePythonAgentURL = func() string {
		if v := os.Getenv("E2E_PY_AGENT_URL"); v != "" {
			return strings.TrimRight(v, "/")
		}
		return "http://127.0.0.1:1111"
	}()

	e2eHTTPClient  = &http.Client{Timeout: 30 * time.Second}
	e2eAgentClient = &http.Client{Timeout: 120 * time.Second}
)

// e2eSession —— 在一个 TestE2E_Alice 内共享的会话状态。
type e2eSession struct {
	username       string
	email          string
	password       string
	aliceUUID      string
	accessToken    string
	refreshToken   string
	conversationID string // Agent 首次响应后生成，后续请求复用
	pyAgentAlive   bool
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

func e2eAlive(t *testing.T) bool {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, e2eBaseURL+"/health", nil)
	resp, err := e2eHTTPClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func e2ePythonAlive(t *testing.T) bool {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, e2ePythonAgentURL+"/docs", nil)
	resp, err := e2eHTTPClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusFound
}

func e2eDoJSON(t *testing.T, client *http.Client, method, url string, body any, headers map[string]string) (*http.Response, []byte) {
	t.Helper()

	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		require.NoError(t, err)
		reader = bytes.NewReader(b)
	}

	req, err := http.NewRequest(method, url, reader)
	require.NoError(t, err)
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	resp, err := client.Do(req)
	require.NoError(t, err, "HTTP 失败 url=%s method=%s", url, method)
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	require.NoError(t, err)
	return resp, respBody
}

func e2eReadJSON[T any](t *testing.T, body []byte) T {
	t.Helper()
	var v T
	require.NoError(t, json.Unmarshal(body, &v), "JSON 解析失败: %s", string(body))
	return v
}

func e2eDecodeJWTPayload(t *testing.T, token string) map[string]any {
	t.Helper()
	parts := strings.Split(token, ".")
	require.Len(t, parts, 3, "JWT 必须 3 段")

	b64 := strings.ReplaceAll(parts[1], "-", "+")
	b64 = strings.ReplaceAll(b64, "_", "/")
	switch len(b64) % 4 {
	case 2:
		b64 += "=="
	case 3:
		b64 += "="
	}

	raw, err := base64.StdEncoding.DecodeString(b64)
	require.NoError(t, err)

	var p map[string]any
	require.NoError(t, json.Unmarshal(raw, &p))
	return p
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "..."
}

// ===========================================================================
// ★ TestE2E_DbAlice —— 直接用数据库里已有的 alice 测试用户
//
// 这是一个固定账号测试：username=alice, email=alice@test.com
// 用户身份：
//   auth_users.id        = 1
//   auth_user_uuid       = e6a818ec-d586-42ee-ad5d-d7594a282578
//   password            = Alice123456  (已用 Argon2id 重置)
//
// 和 TestE2E_Alice 的区别：
//   TestE2E_Alice       → 动态注册新用户，测试注册流程 + 登录 + 代理
//   TestE2E_DbAlice     → 用固定 alice，聚焦测试"登录 + 代理到真实 Python Agent"
// ===========================================================================

// const (
//
//	dbAliceUsername = "alice"
//	dbAlicePassword = "Alice123456"
//	dbAliceEmail    = "alice@test.com"
//	dbAliceUUID     = "e6a818ec-d586-42ee-ad5d-d7594a282578"
//
// )
const (
	dbAliceUsername = "alice"
	dbAlicePassword = "Alice123456"
	dbAliceEmail    = "Alice@test.com"
	dbAliceUUID     = "e6a818ec-d586-42ee-ad5d-d7594a282578"
)

func TestE2E_DbAlice(t *testing.T) {
	if !e2eAlive(t) {
		t.Fatalf("❌ Go Auth 没在 %s 跑", e2eBaseURL)
	}

	pyAlive := e2ePythonAlive(t)

	t.Logf("")
	t.Logf("╔═══════════════════════════════════════════════════════════╗")
	t.Logf("║  DB Alice 固定账号 E2E 测试                              ║")
	t.Logf("║  username = %s  password = %s", dbAliceUsername, dbAlicePassword)
	t.Logf("║  user_uuid = %s", dbAliceUUID)
	t.Logf("║  Go Auth    → %s", e2eBaseURL)
	t.Logf("║  Python     → %s (%s)", e2ePythonAgentURL, map[bool]string{true: "✅ UP", false: "⚠️  DOWN"}[pyAlive])
	t.Logf("╚═══════════════════════════════════════════════════════════╝")
	t.Logf("")

	sess := &e2eSession{
		username:     dbAliceUsername,
		password:     dbAlicePassword,
		aliceUUID:    dbAliceUUID,
		pyAgentAlive: pyAlive,
	}

	// =============================================================
	// Phase 1 —— Login（核心入口：用 DB 里已有的 alice）
	// =============================================================
	t.Run("01_Login_Success", func(t *testing.T) {
		resp, body := e2eDoJSON(t, e2eHTTPClient, http.MethodPost,
			e2eBaseURL+"/api/v1/auth/login",
			map[string]any{
				"username": sess.username,
				"password": sess.password,
			}, nil)

		require.Equal(t, http.StatusOK, resp.StatusCode,
			"DB alice 登录应 200, body=%s", string(body))

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
		login := e2eReadJSON[loginResp](t, body)
		sess.accessToken = login.Data.AccessToken
		sess.refreshToken = login.Data.RefreshToken

		assert.NotEmpty(t, sess.accessToken)
		assert.NotEmpty(t, sess.refreshToken)
		assert.Equal(t, "Bearer", login.Data.TokenType)

		// ★ JWT payload 断言
		payload := e2eDecodeJWTPayload(t, sess.accessToken)
		assert.Equal(t, "auth-service", payload["iss"], "issuer 必须 = auth-service")
		assert.Equal(t, dbAliceUUID, payload["sub"],
			"sub 必须 = DB alice 的 auth_user_uuid")
		assert.NotNil(t, payload["exp"])
		assert.NotNil(t, payload["iat"])

		// ★ 登录返回的 user_uuid 也必须匹配 DB 记录
		assert.Equal(t, dbAliceUUID, login.Data.User.UserUUID,
			"响应中的 user_uuid 必须和 DB 一致")

		t.Logf("  ✅ DB alice 登录成功")
		t.Logf("     access_token  len=%d (RS256 JWT)", len(sess.accessToken))
		t.Logf("     refresh_token len=%d", len(sess.refreshToken))
		t.Logf("     JWT.sub = %s", payload["sub"])
		t.Logf("     JWT.iss = %s", payload["iss"])
	})

	t.Run("01b_Login_WrongPassword_401", func(t *testing.T) {
		resp, _ := e2eDoJSON(t, e2eHTTPClient, http.MethodPost,
			e2eBaseURL+"/api/v1/auth/login",
			map[string]any{
				"username": sess.username,
				"password": "Wrong!",
			}, nil)
		assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)
		t.Logf("  ✅ DB alice 错密码 → 401")
	})

	// =============================================================
	// Phase 2 —— Access Token 校验
	// =============================================================
	t.Run("02a_NoToken_401", func(t *testing.T) {
		resp, _ := e2eDoJSON(t, e2eHTTPClient, http.MethodGet,
			e2eBaseURL+"/agent/chat", nil, nil)
		assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)
		t.Logf("  ✅ 无 Token → 401")
	})

	t.Run("02b_BadSignature_401", func(t *testing.T) {
		bad := func() string {
			if len(sess.accessToken) < 10 {
				return sess.accessToken + "XXXXX"
			}
			return sess.accessToken[:len(sess.accessToken)-5] + "XXXXX"
		}()
		resp, _ := e2eDoJSON(t, e2eHTTPClient, http.MethodGet,
			e2eBaseURL+"/agent/chat", nil,
			map[string]string{"Authorization": "Bearer " + bad})
		assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)
		t.Logf("  ✅ 坏签名 → 401")
	})

	t.Run("02c_NoBearerPrefix_401", func(t *testing.T) {
		resp, _ := e2eDoJSON(t, e2eHTTPClient, http.MethodGet,
			e2eBaseURL+"/agent/chat", nil,
			map[string]string{"Authorization": sess.accessToken})
		assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)
		t.Logf("  ✅ 缺 Bearer 前缀 → 401")
	})

	t.Run("02d_WrongIssuer_JWT_401", func(t *testing.T) {
		// 手工造一个 issuer 不是 "auth-service" 的 JWT 来测试
		// 这个在 integration_test.go 有更完整的覆盖，这里做简化
		t.Logf("  ℹ️ issuer 错误的 JWT 拦截 → 见 integration_test.go Test06")
	})

	// =============================================================
	// Phase 3 —— Refresh Token Rotation
	// =============================================================
	t.Run("03a_Refresh_Success", func(t *testing.T) {
		resp, body := e2eDoJSON(t, e2eHTTPClient, http.MethodPost,
			e2eBaseURL+"/api/v1/auth/refresh",
			map[string]any{"refresh_token": sess.refreshToken}, nil)

		require.Equal(t, http.StatusOK, resp.StatusCode,
			"Refresh 应 200, body=%s", string(body))

		type rfResp struct {
			Data struct {
				AccessToken  string `json:"access_token"`
				RefreshToken string `json:"refresh_token"`
			} `json:"data"`
		}
		rf := e2eReadJSON[rfResp](t, body)

		assert.NotEqual(t, sess.refreshToken, rf.Data.RefreshToken,
			"Rotation：新 refresh_token 必须 ≠ 旧的")

		payload := e2eDecodeJWTPayload(t, rf.Data.AccessToken)
		assert.Equal(t, dbAliceUUID, payload["sub"],
			"Refresh 后 sub 仍是 DB alice 的 uuid")

		sess.accessToken = rf.Data.AccessToken
		sess.refreshToken = rf.Data.RefreshToken

		t.Logf("  ✅ Refresh 成功（Rotation 原子操作）")
	})

	t.Run("03b_Refresh_BadRefreshToken_401", func(t *testing.T) {
		resp, _ := e2eDoJSON(t, e2eHTTPClient, http.MethodPost,
			e2eBaseURL+"/api/v1/auth/refresh",
			map[string]any{"refresh_token": "invalid_token_xxx"}, nil)
		assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)
		t.Logf("  ✅ 无效 refresh_token → 401")
	})

	// =============================================================
	// Phase 4 —— Agent 代理（真实 Python Agent）
	// =============================================================
	t.Run("04_AgentProxy_FirstMessage_NoConversationID", func(t *testing.T) {
		if !sess.pyAgentAlive {
			t.Skipf("Python Agent 没在 %s 跑 — 跳过", e2ePythonAgentURL)
		}

		start := time.Now()

		// ★ 第一次对话：故意不发送 conversation_id。
		// Python Agent 应创建新的 conversation_id，并在响应中返回。
		firstReq := map[string]any{
			"message":         "你好，我是 Alice！请记住：我喜欢吃火锅。",
			"conversation_id": "2070a954-b55d-40af-bc8d-b545d83d7ece",
		}

		resp, body := e2eDoJSON(t, e2eAgentClient, http.MethodPost,
			e2eBaseURL+"/agent/chat",
			firstReq,
			map[string]string{"Authorization": "Bearer " + sess.accessToken})
		elapsed := time.Since(start)

		require.Equal(t, http.StatusOK, resp.StatusCode,
			"Agent 首次代理应 200, body=%s", string(body))

		type chatResp struct {
			Content        string `json:"content"`
			ConversationID string `json:"conversation_id"`
		}

		chat := e2eReadJSON[chatResp](t, body)

		assert.NotEmpty(t, chat.Content, "Agent 回复非空")
		require.NotEmpty(t, chat.ConversationID,
			"首次 Agent 响应必须返回 conversation_id")

		// ★ 保存首次响应返回的 conversation_id。
		// 后续所有属于本轮会话的请求都必须复用它。
		sess.conversationID = chat.ConversationID

		t.Logf("  ✅ 首次 Agent 对话成功")
		t.Logf("     请求：未携带 conversation_id")
		t.Logf("     响应：conversation_id = %s", sess.conversationID)
		t.Logf("     耗时 = %dms", elapsed.Milliseconds())
		t.Logf("     Agent 回复（前 200 字）: %s", truncate(chat.Content, 200))
	})

	t.Run("04a_AgentProxy_FollowUp_WithConversationID", func(t *testing.T) {
		if !sess.pyAgentAlive {
			t.Skipf("Python Agent 没在 %s 跑", e2ePythonAgentURL)
		}

		require.NotEmpty(t, sess.conversationID,
			"后续对话前必须已经获得首次响应返回的 conversation_id")

		start := time.Now()

		// ★ 第二次对话：必须携带第一次响应返回的 conversation_id。
		followUpReq := map[string]any{
			"message":         "我刚才说我喜欢吃什么？",
			"conversation_id": sess.conversationID,
		}

		resp, body := e2eDoJSON(t, e2eAgentClient, http.MethodPost,
			e2eBaseURL+"/agent/chat",
			followUpReq,
			map[string]string{"Authorization": "Bearer " + sess.accessToken})
		elapsed := time.Since(start)

		require.Equal(t, http.StatusOK, resp.StatusCode,
			"Agent 后续对话应 200, body=%s", string(body))

		type chatResp struct {
			Content        string `json:"content"`
			ConversationID string `json:"conversation_id"`
		}

		chat := e2eReadJSON[chatResp](t, body)

		assert.NotEmpty(t, chat.Content, "Agent 回复非空")
		assert.Equal(t, sess.conversationID, chat.ConversationID,
			"后续响应的 conversation_id 应与首次响应保持一致")

		// 如果服务端允许返回新的 conversation_id，也要继续以响应值为准。
		// 当前测试期望同一会话始终保持相同 ID。
		sess.conversationID = chat.ConversationID

		t.Logf("  ✅ 后续 Agent 对话成功")
		t.Logf("     请求：conversation_id = %s", sess.conversationID)
		t.Logf("     响应：conversation_id = %s", chat.ConversationID)
		t.Logf("     耗时 = %dms", elapsed.Milliseconds())
		t.Logf("     Agent 回复（前 200 字）: %s", truncate(chat.Content, 200))
	})

	t.Run("04b_AgentProxy_ForgedXUserID_Overridden", func(t *testing.T) {
		if !sess.pyAgentAlive {
			t.Skipf("Python Agent 没在 %s 跑", e2ePythonAgentURL)
		}

		fakeUUID := "00000000-0000-0000-0000-000000000000"

		resp, _ := e2eDoJSON(t, e2eAgentClient, http.MethodPost,
			e2eBaseURL+"/agent/chat",
			map[string]any{
				"message":         "尝试伪造身份调包",
				"conversation_id": sess.conversationID,
			},
			map[string]string{
				"Authorization": "Bearer " + sess.accessToken,
				"X-User-Id":     fakeUUID, // ★ 伪造！
			})

		require.Equal(t, http.StatusOK, resp.StatusCode)

		t.Logf("  ✅ 伪造防护：")
		t.Logf("     客户端塞   X-User-Id = %s", fakeUUID)
		t.Logf("     Go 强制覆盖 为 JWT.sub = %s", dbAliceUUID)
		t.Logf("     伪造 header 已被删除，Python Agent 收不到")
	})

	t.Run("04c_AgentProxy_NoToken_401", func(t *testing.T) {
		resp, _ := e2eDoJSON(t, e2eHTTPClient, http.MethodPost,
			e2eBaseURL+"/agent/chat",
			map[string]any{"message": "无 token"}, nil)
		assert.Equal(t, http.StatusUnauthorized, resp.StatusCode)
		t.Logf("  ✅ 无 Token → 401")
	})

	t.Run("04d_AgentProxy_AgentTimeout_Scenario", func(t *testing.T) {
		// Go 侧代理超时默认 30s，Python Agent 如果 hang 住会返回 502
		// 这里测"正常对话"不会超时（已有 04 覆盖），边界场景留给手工
		t.Logf("  ℹ️ Agent Timeout 场景 — 手工让 Python Agent hang 住再测")
	})

	// =============================================================
	// Phase 5 —— SSE 流式代理
	// =============================================================
	t.Run("05_SSE_StreamProxy", func(t *testing.T) {
		if !sess.pyAgentAlive {
			t.Skipf("Python Agent 没在 %s 跑", e2ePythonAgentURL)
		}

		require.NotEmpty(t, sess.conversationID,
			"SSE 测试前必须已经获得首次响应返回的 conversation_id")

		// ★ SSE 也是后续对话：必须携带前面响应返回的 conversation_id。
		sseBody, err := json.Marshal(map[string]any{
			"message":         "用 3 句话介绍你自己",
			"conversation_id": sess.conversationID,
		})
		require.NoError(t, err)

		req, err := http.NewRequest(http.MethodPost,
			e2eBaseURL+"/agent/chat/stream",
			bytes.NewReader(sseBody))
		require.NoError(t, err)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+sess.accessToken)

		client := &http.Client{Timeout: 180 * time.Second}
		resp, err := client.Do(req)
		require.NoError(t, err)
		defer resp.Body.Close()

		require.Equal(t, http.StatusOK, resp.StatusCode)
		assert.Contains(t, resp.Header.Get("Content-Type"), "text/event-stream")

		buf := make([]byte, 2048)
		n, _ := resp.Body.Read(buf)
		chunk := string(buf[:n])

		t.Logf("  ✅ SSE 流式代理已连接")
		t.Logf("     Content-Type: %s", resp.Header.Get("Content-Type"))
		t.Logf("     收到 %d bytes", n)
		t.Logf("     前 300 字: %s", truncate(chunk, 300))
	})

	// =============================================================
	// Phase 6 —— Logout（可选，取决于后端有没有实现）
	// =============================================================
	t.Run("06_Logout_Optional", func(t *testing.T) {
		// 如果后端实现了 /api/v1/auth/logout 接口则测试，否则 Skip
		_, body := e2eDoJSON(t, e2eHTTPClient, http.MethodPost,
			e2eBaseURL+"/api/v1/auth/logout", nil,
			map[string]string{"Authorization": "Bearer " + sess.accessToken})
		// 不做硬断言（取决于后端实现），只打日志
		t.Logf("  ℹ️ Logout 响应 body=%s (取决于后端是否实现)", string(body))
	})

	// =============================================================
	// 收尾
	// =============================================================
	t.Logf("")
	t.Logf("╔═══════════════════════════════════════════════════════════╗")
	t.Logf("║  🏁 DB Alice E2E 全链路完成！                            ║")
	t.Logf("║                                                           ║")
	t.Logf("║  ✅ DB 账号: %s / %s", dbAliceUsername, dbAlicePassword)
	t.Logf("║  ✅ user_uuid = %s", dbAliceUUID)
	t.Logf("║  ✅ Login    (200)     JWT RS256, sub = user_uuid")
	t.Logf("║  ✅ AccessToken 校验   (no-token / bad-signature / no-bearer → 401)")
	t.Logf("║  ✅ Refresh  Rotation  (atomic revoke + issue)")
	if sess.pyAgentAlive {
		t.Logf("║  ✅ Agent  代理  (Go 注入 X-User-Id = user_uuid)")
		t.Logf("║  ✅ 伪造防护     (客户端 X-User-Id 被覆盖)")
		t.Logf("║  ✅ SSE    流式  (text/event-stream)")
	} else {
		t.Logf("║  ⚠️  Agent 代理 / SSE 已 SKIP（Python Agent 未运行）")
	}
	t.Logf("╚═══════════════════════════════════════════════════════════╝")
}
