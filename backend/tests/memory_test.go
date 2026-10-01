// Package tests —— E2E 对话链路测试。
//
// 测试目标：
//  1. Login 获取 Alice JWT
//  2. 第一次 chat 不传 conversation_id
//  3. Agent 创建 conversation_id 并在响应中返回
//  4. 后续请求统一使用第一次返回的 conversation_id
//  5. 验证 conversation_id 在整个会话生命周期内保持一致
//  6. 验证 Agent 能够基于同一个 conversation_id 保持上下文
//
// 链路：
//
//	Go Auth :8080
//	     ↓
//	/agent/chat
//	     ↓
//	Python Agent :1111
//	     ↓
//	Redis / PostgreSQL / Kafka
//
// 注意：
//
//	第一次请求故意不传 conversation_id。
//	从第一次响应中获取 conversation_id。
//	后续所有请求都使用这个 conversation_id。
package tests

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const (
	memAliceUsername = "alice"
	memAlicePassword = "Alice123456"
)

// ---------------------------------------------------------------------------
// Chat Response
// ---------------------------------------------------------------------------

type chatResponse struct {
	Content        string `json:"content"`
	ConversationID string `json:"conversation_id"`
}

// ---------------------------------------------------------------------------
// Chat Round
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// doChat
//
// 第一次：conversationID 传空字符串，不发送 conversation_id
//
// 后续：传入第一次响应返回的 conversation_id
// ---------------------------------------------------------------------------

func doChat(
	t *testing.T,
	accessToken string,
	message string,
	conversationID string,
) (content string, responseConversationID string, elapsed time.Duration, statusCode int) {
	t.Helper()

	body := map[string]any{
		"message": message,
	}

	// 只有 conversationID 不为空时才发送。
	//
	// 这保证：
	//
	// Round 1:
	//   {}
	//
	// Round 2+:
	//   {"conversation_id":"xxx"}
	if conversationID != "" {
		body["conversation_id"] = conversationID
	}

	requestBody, err := json.Marshal(body)
	require.NoError(t, err)

	req, err := http.NewRequest(
		http.MethodPost,
		e2eBaseURL+"/agent/chat",
		bytes.NewReader(requestBody),
	)
	require.NoError(t, err)

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+accessToken)

	client := &http.Client{
		Timeout: 60 * time.Second,
	}

	start := time.Now()

	resp, err := client.Do(req)
	require.NoError(t, err)

	defer resp.Body.Close()

	elapsed = time.Since(start)
	statusCode = resp.StatusCode

	responseBody, err := io.ReadAll(resp.Body)
	require.NoError(t, err)

	t.Logf(
		"  HTTP %d | %dms | response=%s",
		resp.StatusCode,
		elapsed.Milliseconds(),
		truncate(string(responseBody), 500),
	)

	require.Equal(
		t,
		http.StatusOK,
		resp.StatusCode,
		"Agent chat 应返回 200",
	)

	var parsed chatResponse

	err = json.Unmarshal(responseBody, &parsed)
	require.NoError(t, err)

	require.NotEmpty(
		t,
		parsed.Content,
		"Agent content 不应该为空",
	)

	require.NotEmpty(
		t,
		parsed.ConversationID,
		"Agent 必须返回 conversation_id",
	)

	return parsed.Content, parsed.ConversationID, elapsed, statusCode
}

// ---------------------------------------------------------------------------
// TestE2E_DbAlice_ConversationLifecycle
// ---------------------------------------------------------------------------

func TestE2E_DbAlice_ConversationLifecycle(t *testing.T) {

	// =====================================================================
	// 1. 服务健康检查
	// =====================================================================

	if !e2eAlive(t) {
		t.Fatalf(
			"Go Auth 没有运行：%s",
			e2eBaseURL,
		)
	}

	if !e2ePythonAlive(t) {
		t.Skipf(
			"Python Agent 没有运行：%s",
			e2ePythonAgentURL,
		)
	}

	t.Log("")
	t.Log("╔══════════════════════════════════════════════════════════╗")
	t.Log("║        E2E Conversation Lifecycle Test                  ║")
	t.Log("╠══════════════════════════════════════════════════════════╣")
	t.Log("║  Round 1  → 不传 conversation_id                       ║")
	t.Log("║  Round 2  → 使用 Round 1 返回的 conversation_id       ║")
	t.Log("║  Round 3  → 使用同一个 conversation_id                ║")
	t.Log("║  Round 4  → 使用同一个 conversation_id                ║")
	t.Log("╚══════════════════════════════════════════════════════════╝")
	t.Log("")

	// =====================================================================
	// 2. Login
	// =====================================================================

	t.Log("── Login ──")

	resp, body := e2eDoJSON(
		t,
		e2eHTTPClient,
		http.MethodPost,
		e2eBaseURL+"/api/v1/auth/login",
		map[string]any{
			"username": memAliceUsername,
			"password": memAlicePassword,
		},
		nil,
	)

	require.Equal(
		t,
		http.StatusOK,
		resp.StatusCode,
	)

	type loginResponse struct {
		Data struct {
			AccessToken string `json:"access_token"`
		} `json:"data"`
	}

	login := e2eReadJSON[loginResponse](t, body)

	token := login.Data.AccessToken

	require.NotEmpty(
		t,
		token,
		"Login 必须返回 access_token",
	)

	t.Logf(
		"  ✅ Login OK，token length=%d",
		len(token),
	)

	// =====================================================================
	// 3. Round 1
	//
	// 关键：
	//
	// conversationID = ""
	//
	// 请求 JSON：
	//
	// {
	//   "message": "..."
	// }
	//
	// 不包含 conversation_id。
	// =====================================================================

	t.Log("")
	t.Log("════════ Round 1 ════════")
	t.Log("第一次请求：不传 conversation_id")

	message1 := "你好，我叫 Alice，我住在北京。请记住我的名字和城市。"

	content1, conversationID, elapsed1, status1 := doChat(
		t,
		token,
		message1,
		"",
	)

	require.Equal(
		t,
		http.StatusOK,
		status1,
	)

	require.NotEmpty(
		t,
		conversationID,
		"Round 1 必须由 Agent 创建 conversation_id",
	)

	t.Logf(
		"  Message: %s",
		message1,
	)

	t.Logf(
		"  Agent: %s",
		truncate(content1, 300),
	)

	t.Logf(
		"  conversation_id: %s",
		conversationID,
	)

	t.Logf(
		"  elapsed: %dms",
		elapsed1.Milliseconds(),
	)

	// 保存第一次返回的 conversation_id。
	//
	// 后面的请求全部使用这个 ID。
	firstConversationID := conversationID

	// 等待 Kafka / Redis 异步链路。
	time.Sleep(2500 * time.Millisecond)

	// =====================================================================
	// 4. Round 2
	//
	// 使用 Round 1 返回的 conversation_id。
	// =====================================================================

	t.Log("")
	t.Log("════════ Round 2 ════════")
	t.Log("第二次请求：使用 Round 1 返回的 conversation_id")

	message2 := "我刚才告诉你我的名字和居住城市分别是什么？"

	content2, conversationID2, elapsed2, status2 := doChat(
		t,
		token,
		message2,
		firstConversationID,
	)

	require.Equal(
		t,
		http.StatusOK,
		status2,
	)

	require.NotEmpty(t, content2)
	require.NotEmpty(t, conversationID2)

	t.Logf(
		"  Message: %s",
		message2,
	)

	t.Logf(
		"  Agent: %s",
		truncate(content2, 300),
	)

	t.Logf(
		"  conversation_id: %s",
		conversationID2,
	)

	// ---------------------------------------------------------------------
	// conversation_id 必须保持一致
	// ---------------------------------------------------------------------

	assert.Equal(
		t,
		firstConversationID,
		conversationID2,
		"Round 2 必须返回 Round 1 相同的 conversation_id",
	)

	// ---------------------------------------------------------------------
	// 记忆验证
	// ---------------------------------------------------------------------

	reply2 := strings.ToLower(content2)

	assert.Contains(
		t,
		reply2,
		"alice",
		"Round 2 应该记住 Alice",
	)

	assert.Contains(
		t,
		content2,
		"北京",
		"Round 2 应该记住北京",
	)

	time.Sleep(2500 * time.Millisecond)

	// =====================================================================
	// 5. Round 3
	//
	// 继续使用第一次返回的 conversation_id。
	// =====================================================================

	t.Log("")
	t.Log("════════ Round 3 ════════")
	t.Log("第三次请求：继续使用同一个 conversation_id")

	message3 := "我最喜欢吃火锅。请告诉我，到目前为止你记住了我的哪些信息？"

	content3, conversationID3, elapsed3, status3 := doChat(
		t,
		token,
		message3,
		firstConversationID,
	)

	require.Equal(
		t,
		http.StatusOK,
		status3,
	)

	require.NotEmpty(t, content3)
	require.NotEmpty(t, conversationID3)

	t.Logf(
		"  Message: %s",
		message3,
	)

	t.Logf(
		"  Agent: %s",
		truncate(content3, 500),
	)

	t.Logf(
		"  conversation_id: %s",
		conversationID3,
	)

	// ---------------------------------------------------------------------
	// conversation_id 一致性
	// ---------------------------------------------------------------------

	assert.Equal(
		t,
		firstConversationID,
		conversationID3,
		"Round 3 必须继续使用 Round 1 的 conversation_id",
	)

	// ---------------------------------------------------------------------
	// 记忆验证
	// ---------------------------------------------------------------------

	reply3 := strings.ToLower(content3)

	assert.Contains(
		t,
		reply3,
		"alice",
		"Round 3 应该记住 Alice",
	)

	assert.Contains(
		t,
		content3,
		"北京",
		"Round 3 应该记住北京",
	)

	assert.Contains(
		t,
		content3,
		"火锅",
		"Round 3 应该记住火锅",
	)

	time.Sleep(2500 * time.Millisecond)

	// =====================================================================
	// 6. Round 4
	//
	// 不再添加新信息，只验证整个历史上下文。
	// =====================================================================

	t.Log("")
	t.Log("════════ Round 4 ════════")
	t.Log("第四次请求：继续使用同一个 conversation_id")

	message4 := "请再次总结一下你记住的关于我的信息。"

	content4, conversationID4, elapsed4, status4 := doChat(
		t,
		token,
		message4,
		firstConversationID,
	)

	require.Equal(
		t,
		http.StatusOK,
		status4,
	)

	require.NotEmpty(t, content4)
	require.NotEmpty(t, conversationID4)

	t.Logf(
		"  Message: %s",
		message4,
	)

	t.Logf(
		"  Agent: %s",
		truncate(content4, 500),
	)

	t.Logf(
		"  conversation_id: %s",
		conversationID4,
	)

	// ---------------------------------------------------------------------
	// 最终验证
	// ---------------------------------------------------------------------

	assert.Equal(
		t,
		firstConversationID,
		conversationID4,
		"Round 4 必须继续使用同一个 conversation_id",
	)

	// =====================================================================
	// 7. 最终报告
	// =====================================================================

	t.Log("")
	t.Log("╔══════════════════════════════════════════════════════════╗")
	t.Log("║                  E2E TEST PASSED                        ║")
	t.Log("╠══════════════════════════════════════════════════════════╣")
	t.Logf("║ conversation_id = %s", firstConversationID)
	t.Log("║")
	t.Logf("║ Round 1 = %dms", elapsed1.Milliseconds())
	t.Logf("║ Round 2 = %dms", elapsed2.Milliseconds())
	t.Logf("║ Round 3 = %dms", elapsed3.Milliseconds())
	t.Logf("║ Round 4 = %dms", elapsed4.Milliseconds())
	t.Log("║")
	t.Log("║ conversation_id 生命周期验证：OK")
	t.Log("║ 多轮上下文记忆验证：OK")
	t.Log("╚══════════════════════════════════════════════════════════╝")
}
