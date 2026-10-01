from pydantic import BaseModel


class ChatResponse(BaseModel):
    """普通聊天接口的响应。"""

    content: str
    conversation_id: str
