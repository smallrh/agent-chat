package handler

import (
	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/client"
)

// AttachmentHandler 附件 BFF：/api/v1/attachments/* → Agent /attachments/*
//
// 两个端点的包装方式**故意不同**，原因见下：
//
//   - Upload：multipart/form-data 上传，返回附件元数据 JSON。
//     走统一信封 —— 前端拿到 data.attachment_id 再去发消息。
//     注意 body 是二进制 multipart（内含随机 boundary），
//     BFFProxy 会原样透传 Content-Type，boundary 不会被破坏。
//
//   - Download：返回图片/文件的原始字节流。走 Raw 透传 ——
//     套一层 {code,message,data} 会让 <img src="..."> 和 <a download>
//     直接失效（浏览器拿到的是 JSON 不是图片）。
type AttachmentHandler struct {
	proxy *BFFProxy
}

func NewAttachmentHandler(agent *client.AgentClient) *AttachmentHandler {
	return &AttachmentHandler{proxy: NewBFFProxy(agent)}
}

// Upload 上传附件（multipart，字段名 file）。
func (h *AttachmentHandler) Upload(c *gin.Context) {
	h.proxy.JSON(c, "/attachments", "")
}

// Download 下载/预览附件（带 ?inline=1 时 Agent 返回 inline  disposition）。
func (h *AttachmentHandler) Download(c *gin.Context) {
	h.proxy.Raw(c, "/attachments/"+c.Param("id"), c.Request.URL.RawQuery)
}
