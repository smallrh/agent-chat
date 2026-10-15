package handler

import (
	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/client"
)

// TaskHandler 任务 BFF：/api/v1/tasks/* → Agent /tasks/*
//
// 意义（对应《现状和不足.md》P2「真任务队列 + 任务面板」）：
// 任务接口此前只能走 /agent/tasks（另一套前缀 + FastAPI 原始 JSON + 裸 SSE），
// 前端要记两套前缀、两套错误格式，SSE 也没法复用统一的鉴权/刷新逻辑。
// 现在与会话、记忆一起统一在 /api/v1 下。
//
// 注意 Stream 是**唯一**不能包信封的端点 —— SSE 必须原样透传，
// 否则事件会被 gin 的写缓冲攒批，进度条变成「卡住不动 → 突然 100%」。
type TaskHandler struct {
	proxy *BFFProxy
}

func NewTaskHandler(agent *client.AgentClient) *TaskHandler {
	return &TaskHandler{proxy: NewBFFProxy(agent)}
}

// List 任务列表。
// 带 conversation_id → 会话内任务面板；不带 → 任务中心（该用户全部会话）。
// 支持 ?status=&kind=&limit=&offset= 原样透传给 Agent。
func (h *TaskHandler) List(c *gin.Context) {
	h.proxy.JSON(c, "/tasks", c.Request.URL.RawQuery)
}

// Kinds 已注册的任务类型（demo / research …），前端渲染下拉框用。
// 必须声明在 /tasks/:id 之前，否则 "kinds" 会被当成 task_id。
func (h *TaskHandler) Kinds(c *gin.Context) {
	h.proxy.JSON(c, "/tasks/kinds", "")
}

// Create 创建任务并立即后台运行（返回 task_id，不阻塞）。
func (h *TaskHandler) Create(c *gin.Context) {
	h.proxy.JSON(c, "/tasks", "")
}

// Get 单个任务详情（含 progress / step / result）。
func (h *TaskHandler) Get(c *gin.Context) {
	h.proxy.JSON(c, "/tasks/"+c.Param("id"), "")
}

// Cancel 取消一个进行中的任务。
func (h *TaskHandler) Cancel(c *gin.Context) {
	h.proxy.JSON(c, "/tasks/"+c.Param("id")+"/cancel", "")
}

// Retry 重跑一个已终态任务（failed / cancelled / 重启中断）。
func (h *TaskHandler) Retry(c *gin.Context) {
	h.proxy.JSON(c, "/tasks/"+c.Param("id")+"/retry", "")
}

// Stream 订阅某会话的任务事件流（SSE 原样透传）。
// 事件：task.created / running / progress / completed / failed / cancelled。
func (h *TaskHandler) Stream(c *gin.Context) {
	h.proxy.Stream(c, "/tasks/stream", c.Request.URL.RawQuery)
}
