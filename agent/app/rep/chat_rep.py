from pydantic import BaseModel


class ChatResponse(BaseModel):
    """普通聊天接口的响应。"""

    content: str
    conversation_id: str
    # 非流式也带 run_id：便于前端对「卡住的长请求」调用 POST /chat/cancel。
    # 取消不会回滚已经产生的内容 —— 多步工具循环会在下一轮检查点退出，
    # 已生成部分照常落库并返回。
    run_id: str | None = None
