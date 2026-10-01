from app.agent.agent import Agent
from app.req.chat_req import ChatRequest
from app.rep.chat_rep import ChatResponse
from app.service.message_service import message_service
from app.utils.sse import sse


class ChatService:

    def __init__(self):
        self.agent = Agent()

    async def chat(
        self,
        request: ChatRequest,
    ) -> ChatResponse:

        # 1. 调用 Agent
        result = self.agent.chat(
            message=request.message,
        )

        final_content = result["content"]

        # 2. 保存用户消息
        await message_service.create_user_message(
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            content=request.message,
        )

        # 3. 保存 AI 消息
        await message_service.create_assistant_message(
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            content=final_content,
        )

        # 4. 返回
        return ChatResponse(
            content=final_content,
        )

    async def stream_chat(
        self,
        request: ChatRequest,
    ):
        """
        SSE 流式聊天。

        Agent 负责：
        - LLM
        - Tool Calling
        - Tool 执行

        ChatService 负责：
        - SSE
        - 消息事件
        """

        final_content = ""

        async for event in self.agent.stream(
            message=request.message,
        ):

            event_type = event.get("type")

            # 普通消息
            if event_type == "message":
                content = event.get("content", "")

                final_content += content

                yield sse({
                    "type": "message",
                    "content": content,
                })

            # Tool Calling
            elif event_type == "tool_call":

                yield sse(event)

            # Tool 执行中
            elif event_type == "tool_executing":

                yield sse(event)

            # Tool 执行结果
            elif event_type == "tool_result":

                yield sse(event)

            # Agent 完成
            elif event_type == "done":

                # 保存用户消息
                await message_service.create_user_message(
                    user_id=request.user_id,
                    conversation_id=request.conversation_id,
                    content=request.message,
                )

                # 保存 AI 消息
                await message_service.create_assistant_message(
                    user_id=request.user_id,
                    conversation_id=request.conversation_id,
                    content=final_content,
                )

                yield sse("[DONE]")