import logging
import uuid

from app.agent.agent import Agent
from app.repo.message_repo import MessageRepository
from app.repo.redis_repo import RedisRepository
from app.req.chat_req import ChatRequest
from app.rep.chat_rep import ChatResponse
from app.service.message_service import message_service
from app.service.retrieval_service import memory_retrieval_service
from app.utils.sse import sse


logger = logging.getLogger(__name__)


class ChatService:

    def __init__(self):
        self.agent = Agent()
        # 只读 Redis（ChatService 不写 Redis，Kafka consumer 会写）
        self._redis = RedisRepository()
        self._redis_started = False
        # conversation ownership 校验用（MessageRepository 自己维护 PG 连接池）
        self._msg_repo = MessageRepository()
        self._msg_repo_started = False

    # ----------------------------------------------------------
    # Redis 懒启动
    # ----------------------------------------------------------

    async def _ensure_redis(self):
        if not self._redis_started:
            await self._redis.start()
            self._redis_started = True

    async def _ensure_msg_repo(self):
        """懒启动 MessageRepository（供 ownership 校验用）。"""
        if not self._msg_repo_started:
            await self._msg_repo.start()
            self._msg_repo_started = True

    # ----------------------------------------------------------
    # 读上下文（summary + history）
    # ----------------------------------------------------------

    async def _load_context(
        self,
        conversation_id: str,
    ) -> tuple[str | None, list[dict]]:
        """
        从 Redis 读对话 summary + 最近 20 条历史消息。

        返回 (summary, openai_history)：
          - summary: str 或 None
          - openai_history: list[{"role": ..., "content": ...}]
        """
        summary = None
        history: list[dict] = []

        try:
            await self._ensure_redis()

            # 1) 摘要
            summary = await self._redis.get_summary(conversation_id)

            # 2) 最近消息 → 转 OpenAI message 格式
            raw = await self._redis.get_recent_messages(
                conversation_id,
                limit=20,
            )
            for m in raw:
                role = m.get("role")
                content = m.get("content", "")
                if role and role in ("user", "assistant", "system"):
                    history.append({"role": role, "content": content})

        except Exception:
            # Redis 读失败不能阻断对话，降级为无上下文
            logger.exception(
                "ChatService._load_context FAILED: conversation_id=%s",
                conversation_id,
            )

        logger.info(
            "ChatService._load_context: conv=%s summary=%s history=%d",
            conversation_id,
            f"{len(summary)}chars" if summary else None,
            len(history),
        )
        return summary, history

    # ----------------------------------------------------------
    # 读取端：每次用户发言时检索长期记忆（供 Agent 注入上下文）
    # ----------------------------------------------------------

    async def _retrieve_memories(self, user_id: int, query: str) -> list[dict]:
        """
        读取端核心：按用户当前问题语义检索长期记忆。

        - 失败（Qdrant / PG / LM Studio 抖动）一律降级为 [] —— 记忆是"增强"不是
          "必须"，绝不能因记忆检索失败而阻断对话。
        - record_access=True：真实读取端命中即计数（独立 write，不计入回表 read）。
        """
        try:
            return await memory_retrieval_service.retrieve(
                user_id,
                query,
                record_access=True,
            )
        except Exception:
            logger.exception(
                "ChatService._retrieve_memories FAILED (degrade to no memory): "
                "user_id=%d query=%.40s",
                user_id, query,
            )
            return []

    # ----------------------------------------------------------
    # conversation_id 解析
    # ----------------------------------------------------------

    @staticmethod
    def _resolve_conversation_id(request: ChatRequest) -> str:
        """确定本次请求使用的 conversation_id。

        - 前端传了 → 直接用（继续已有会话）
        - 没传 → 后端生成新 UUID（新建会话）
        """
        if request.conversation_id:
            return request.conversation_id
        return str(uuid.uuid4())

    # ----------------------------------------------------------
    # 非流式
    # ----------------------------------------------------------

    async def chat(
        self,
        request: ChatRequest,
        *,
        user_id: int,
    ) -> ChatResponse:

        conversation_id = self._resolve_conversation_id(request)

        # 0. ownership 校验 —— 拿不到权限直接 403，不读 Redis 不落库
        await self._ensure_msg_repo()
        await self._msg_repo.ensure_conversation_owner(
            conversation_id=conversation_id,
            user_id=user_id,
        )

        # 1. 读 Redis 上下文（summary + history）
        summary, history = await self._load_context(conversation_id)

        # 1.5 读取端：按当前问题检索长期记忆，注入 Agent 上下文
        memories = await self._retrieve_memories(user_id, request.message)

        # 2. 调 Agent（带上历史、摘要、检索到的记忆）
        result = self.agent.chat(
            message=request.message,
            history=history,
            summary=summary,
            memories=memories,
        )

        final_content = result["content"]

        # 2. 保存用户消息
        await message_service.create_user_message(
            user_id=user_id,
            conversation_id=conversation_id,
            content=request.message,
        )

        # 3. 保存 AI 消息
        await message_service.create_assistant_message(
            user_id=user_id,
            conversation_id=conversation_id,
            content=final_content,
        )

        # 4. 返回
        return ChatResponse(
            content=final_content,
            conversation_id=conversation_id,
        )

    # ----------------------------------------------------------
    # conversation ownership 校验（路由层用）
    # ----------------------------------------------------------

    async def ensure_conversation_owner(
        self,
        request: ChatRequest,
        *,
        user_id: int,
    ) -> str:
        """拿到 conversation_id 并校验归属。

        路由层在返回 StreamingResponse 之前调用，保证 async generator
        里的 HTTPException 不会被静默吞掉。

        Returns:
            确定好的 conversation_id（可能是新生成的）。

        Raises:
            HTTPException(403):  conversation 不属于当前 user。
        """
        conversation_id = self._resolve_conversation_id(request)
        await self._ensure_msg_repo()
        await self._msg_repo.ensure_conversation_owner(
            conversation_id=conversation_id,
            user_id=user_id,
        )
        return conversation_id

    # ----------------------------------------------------------
    # 流式
    # ----------------------------------------------------------

    async def stream_chat(
        self,
        request: ChatRequest,
        *,
        user_id: int,
        conversation_id: str,
    ):
        """
        SSE 流式聊天（完整事件协议）。

        conversation_id 必须由路由层先通过 ensure_conversation_owner()
        校验过再传入——async generator 里抛 HTTPException 不会被 FastAPI
        正确转成 403 响应，必须在 generator 外层完成校验。

        ChatService 负责：
        - 读 Redis 上下文（summary + history）传给 Agent
        - 发 agent.start / message.start / message.end / agent.end / done
        - 补全每个事件的 conversation_id / message_id / timestamp
        - 消息落库（Agent 结束后）

        Agent 负责：纯执行事件流，不管 SSE 信封。
        """

        assistant_message_id = str(uuid.uuid4())

        final_content = ""

        # ====== 读 Redis 上下文 ======
        summary, history = await self._load_context(conversation_id)

        # ====== 读取端：检索长期记忆注入上下文 ======
        memories = await self._retrieve_memories(user_id, request.message)

        # ====== 头部事件 ======
        yield sse(
            "agent.start",
            {"conversation_id": conversation_id},
        )

        yield sse(
            "message.start",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "role": "assistant",
            },
        )

        # ====== Agent 事件循环 ======
        try:
            async for event in self.agent.stream(
                message=request.message,
                history=history,
                summary=summary,
                memories=memories,
            ):
                event_type = event.get("type")

                if event_type == "message.delta":
                    final_content += event.get("content", "")
                    yield sse(
                        "message.delta",
                        {
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            "content": event.get("content", ""),
                        },
                    )

                elif event_type == "tool.start":
                    yield sse(
                        "tool.start",
                        {
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            "tool_call_id": event.get("tool_call_id"),
                            "tool_name": event.get("tool_name"),
                            "arguments": event.get("arguments", {}),
                        },
                    )

                elif event_type == "tool.result":
                    yield sse(
                        "tool.result",
                        {
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            "tool_call_id": event.get("tool_call_id"),
                            "tool_name": event.get("tool_name"),
                            "result": event.get("result"),
                        },
                    )

                elif event_type == "tool.end":
                    yield sse(
                        "tool.end",
                        {
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            "tool_call_id": event.get("tool_call_id"),
                            "tool_name": event.get("tool_name"),
                        },
                    )

                elif event_type == "agent.end":
                    # Agent 内部结束，这里不直接透传成 SSE 的 agent.end，
                    # 而是先等我们发完 message.end + agent.end + 落库，
                    # 最后再发 done
                    pass

                else:
                    logger.warning(
                        "Unknown agent event type: %s", event_type
                    )

        except Exception as exc:
            logger.exception("Agent stream error")
            yield sse(
                "error",
                {
                    "conversation_id": conversation_id,
                    "message_id": assistant_message_id,
                    "error": str(exc),
                },
            )

        # ====== 尾部事件 ======
        yield sse(
            "message.end",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "finish_reason": "stop",
            },
        )

        yield sse(
            "agent.end",
            {"conversation_id": conversation_id},
        )

        # ====== 消息落库（Kafka） ======
        # 放在最后发 done 之前，保证前端收到 done 时消息已经持久化
        try:
            await message_service.create_user_message(
                user_id=user_id,
                conversation_id=conversation_id,
                content=request.message,
            )
            await message_service.create_assistant_message(
                user_id=user_id,
                conversation_id=conversation_id,
                content=final_content,
            )
        except Exception:
            # 落库失败只记日志，不要阻断 SSE 尾部事件（前端总要收到 done）
            logger.exception("Failed to persist stream messages")

        yield sse(
            "done",
            {"conversation_id": conversation_id},
        )


chat_service = ChatService()
