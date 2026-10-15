package handler

import (
	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/client"
)

// ConversationHandler 通用会话 API（BFF）：鉴权后转发 Agent 核心会话接口，并包装成统一信封。
type ConversationHandler struct {
	proxy *BFFProxy
}

func NewConversationHandler(agent *client.AgentClient) *ConversationHandler {
	return &ConversationHandler{proxy: NewBFFProxy(agent)}
}

func (h *ConversationHandler) List(c *gin.Context) {
	h.proxy.JSON(c, "/conversations", c.Request.URL.RawQuery)
}

func (h *ConversationHandler) ListMessages(c *gin.Context) {
	id := c.Param("id")
	h.proxy.JSON(c, "/conversations/"+id+"/messages", c.Request.URL.RawQuery)
}

func (h *ConversationHandler) Rename(c *gin.Context) {
	id := c.Param("id")
	h.proxy.JSON(c, "/conversations/"+id, "")
}

func (h *ConversationHandler) Delete(c *gin.Context) {
	id := c.Param("id")
	h.proxy.JSON(c, "/conversations/"+id, "")
}
