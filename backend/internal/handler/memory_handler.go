package handler

import (
	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/client"
)

// MemoryHandler 记忆 BFF：/api/v1/memories/* → Agent /memories/*
//
// 意义（对应《现状和不足.md》Backend 缺点 1「BFF 不完整」）：
// 以前记忆接口只能走 /agent/memories（另一套前缀 + FastAPI 原始 JSON），
// 前端要记两套前缀、两套错误格式。现在与会话 API 一起统一在 /api/v1 下，
// 鉴权、信封、错误码完全一致。
type MemoryHandler struct {
	proxy *BFFProxy
}

func NewMemoryHandler(agent *client.AgentClient) *MemoryHandler {
	return &MemoryHandler{proxy: NewBFFProxy(agent)}
}

// List 记忆列表（支持 ?memory_type=&limit=&offset= 透传给 Agent）
func (h *MemoryHandler) List(c *gin.Context) {
	h.proxy.JSON(c, "/memories", c.Request.URL.RawQuery)
}

// Delete 软删一条记忆（Agent 侧会级联清理 Qdrant / Redis）
func (h *MemoryHandler) Delete(c *gin.Context) {
	id := c.Param("id")
	h.proxy.JSON(c, "/memories/"+id, "")
}

// Source 记忆溯源：这条记忆是从哪句话抽出来的（排查「为什么会有这条记忆」用）
func (h *MemoryHandler) Source(c *gin.Context) {
	id := c.Param("id")
	h.proxy.JSON(c, "/memories/"+id+"/source", "")
}
