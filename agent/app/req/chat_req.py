from pydantic import BaseModel


class ChatRequest(BaseModel):
    """聊天请求。

    message_id 由后端统一生成，前端不必传入。
    conversation_id 可选：不传代表新建会话，由后端生成；
    传入则表示继续已有会话。

    注意：**user_id 已移除**。用户身份由 Go Auth Service 通过
    X-User-Id Header（UUID string）注入，Python Agent 在入口层
    从 Header 读取并转换成 users.id（int8）。
    """

    message: str
    conversation_id: str | None = None
    # user_id 从 ChatRequest 移除 —— 见 app/core/auth.py 的 get_current_user_id
