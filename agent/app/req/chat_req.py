from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """聊天请求。

    message_id 由后端统一生成，前端不必传入。
    conversation_id 可选：不传代表新建会话，由后端生成；
    传入则表示继续已有会话。

    注意：**user_id 已移除**。用户身份由 Go Auth Service 通过
    X-User-Id Header（UUID string）注入，Python Agent 在入口层
    从 Header 读取并转换成 users.id（int8）。

    attachment_ids（P2 多模态）：本条消息携带的附件。附件必须先通过
    POST /attachments 上传拿到 id，再随消息一起提交 —— 后端会据此
    加载文件内容并注入模型上下文（文本拼进 user message，
    图片走 OpenAI 多模态 content part）。
    """

    message: str
    conversation_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)
    # user_id 从 ChatRequest 移除 —— 见 app/core/auth.py 的 get_current_user_id
