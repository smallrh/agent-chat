import asyncio
import json
import logging
import uuid

from app.agent.agent import Agent
from app.agent.cancel import AgentCancelled, create_token, remove_token
from app.repo.message_repo import MessageRepository
from app.repo.redis_repo import RedisRepository
from app.req.chat_req import ChatRequest
from app.rep.chat_rep import ChatResponse
from app.service.attachment_service import attachment_service
from app.service.conversation_service import conversation_service
from app.service.usage_service import METRIC_CHAT, METRIC_SEARCH, usage_service
from app.service.grounding import enforce as enforce_grounding
from app.service.message_service import message_service
from app.service.profile_service import ProfileService
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
        # 用户画像：跨会话常驻事实。复用 ChatService 自己的 Redis 连接（懒启动）
        self.profile_service = ProfileService(self._redis)

    # ----------------------------------------------------------
    # Redis 懒启动
    # ----------------------------------------------------------

    async def _ensure_redis(self):
        if self._redis is None:
            self._redis = RedisRepository()
        if not self._redis_started:
            await self._redis.start()
            self._redis_started = True

    async def _ensure_msg_repo(self):
        """懒启动 MessageRepository（供 ownership 校验用）。"""
        # aclose() 会把引用置 None，这里兜底重建，避免关闭后被调用直接炸
        if self._msg_repo is None:
            self._msg_repo = MessageRepository()
        if not self._msg_repo_started:
            await self._msg_repo.start()
            self._msg_repo_started = True

    async def aclose(self):
        """
        释放 ChatService 持有的连接资源，由 lifespan shutdown 调用。

        PG 连接走的是共享池（app/repo/pg.py），由 lifespan 统一关闭，这里只解引用；
        Redis 是 ChatService 自己的 client，必须在这里关掉 —— 否则 RELOAD=true
        每次改文件重建进程都会漏掉一组 Redis 连接。
        """
        if self._redis_started and self._redis is not None:
            try:
                await self._redis.stop()
            except Exception:
                logger.exception("ChatService Redis close failed (ignored)")
            self._redis_started = False

        # 共享 PG 池不在这里关，解引用即可（见 MemoryRepository.stop 的约定）
        self._msg_repo = None
        self._msg_repo_started = False
        logger.info("ChatService closed")

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
    # 用户画像（跨会话常驻事实）
    # ----------------------------------------------------------

    async def _load_profile(self, user_id: int) -> dict:
        """
        读用户画像，供 Agent 无条件注入 system prompt。

        与「按当前问题语义检索记忆」互补：
          - 语义检索 → 按需召回，可能召回不到（新会话闲聊开场时尤其明显）
          - profile    → 无条件常驻，保证"用户明确说过的基础信息"每轮都可见

        失败一律降级为 {}（画像只是增强，不能阻断对话）。
        """
        try:
            await self._ensure_redis()
            return await self.profile_service.get_profile(user_id) or {}
        except Exception:
            logger.exception(
                "ChatService._load_profile FAILED (degrade to empty): user_id=%d",
                user_id,
            )
            return {}

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
    # 附件（P2 多模态）
    # ----------------------------------------------------------

    async def _prepare_attachments(
        self,
        user_id: int,
        attachment_ids: list[str],
    ) -> tuple[str, list[dict], list[dict]]:
        """
        加载附件并构造「可注入模型的内容」。

        返回 (attachment_text, image_parts, metas)：
          - attachment_text：追加到用户消息尾部的文本块
          - image_parts：OpenAI 多模态内容块（图片）
          - metas：写进 message.metadata.attachments 的精简元数据（前端回显用）

        附件服务不可用 / 单个 id 非法都只降级，绝不阻断对话 ——
        「聊不了天」比「这一轮看不到附件」严重得多。
        """
        if not attachment_ids:
            return "", [], []
        try:
            rows = await attachment_service.load_for_message(
                user_id=user_id, attachment_ids=attachment_ids,
            )
        except Exception:
            logger.exception(
                "ChatService._prepare_attachments FAILED (degrade to no attachment)"
            )
            return "", [], []
        if not rows:
            return "", [], []
        text, image_parts = attachment_service.build_context(rows)
        metas = [attachment_service.public_meta(r) for r in rows]
        return text, image_parts, metas

    @staticmethod
    def _user_metadata(metas: list[dict]) -> dict | None:
        """用户消息的 metadata：只有真的带附件时才写，避免每条消息都塞空对象。"""
        return {"attachments": metas} if metas else None

    async def _bind_attachments(
        self,
        user_id: int,
        attachment_ids: list[str],
        message_id: str,
        conversation_id: str,
    ) -> None:
        if not attachment_ids:
            return
        try:
            await attachment_service.bind_all(
                user_id=user_id,
                attachment_ids=attachment_ids,
                message_id=message_id,
                conversation_id=conversation_id,
            )
        except Exception:
            logger.exception("ChatService._bind_attachments FAILED (ignored)")

    @staticmethod
    async def _record_usage(user_id: int, tool_traces: list[dict]) -> None:
        """
        P3 计费埋点：一轮对话 = 1 次 chat；其中每发生一次联网检索再计 web_search。

        刻意放在**本轮结束**而不是开头：LLM 挂掉或被取消的一轮没有真实产出，
        不该计费。埋点失败由 UsageService 内部吞掉，绝不影响对话。
        """
        await usage_service.incr(user_id, METRIC_CHAT)
        searches = sum(
            1 for t in (tool_traces or []) if t.get("name") == "web_search"
        )
        if searches:
            await usage_service.incr(user_id, METRIC_SEARCH, searches)

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

        # 1.6 用户画像：跨会话常驻事实（无条件注入，不依赖语义召回）
        profile = await self._load_profile(user_id)

        # 1.7 附件：加载文件内容，构造注入模型的文本块 + 图片内容块
        attachment_text, image_parts, attachment_metas = (
            await self._prepare_attachments(user_id, request.attachment_ids)
        )

        # 2. 调 Agent（同步 LLM 放到线程，避免堵住事件循环）
        #    非流式也建 run token：把 run_id 带回给前端，必要时可以
        #    POST /chat/cancel 让多步工具循环在下一检查点退出。
        #    finally 里保证 remove_token，避免 cancel registry 泄漏。
        token = create_token(user_id)
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.to_thread(
                self.agent.chat,
                request.message,
                history,
                summary,
                memories,
                # 注意：profile 插在 memories 之后，必须用关键字传参，
                # 否则 user_id 会被位置参数错位塞进 profile。
                profile=profile,
                user_id=user_id,
                conversation_id=conversation_id,
                loop=loop,
                cancel_token=token,
                attachment_text=attachment_text,
                image_parts=image_parts or None,
            )
        finally:
            remove_token(token.run_id)

        # grounding：引用强制 + 检索无果时追加「未核实」说明
        tool_traces = result.get("tool_traces") or []
        final_content = enforce_grounding(result["content"], tool_traces)

        # 2. 保存用户消息（带附件元数据，刷新后前端能把图片/文件卡片渲染回来）
        user_message_id = await message_service.create_user_message(
            user_id=user_id,
            conversation_id=conversation_id,
            content=request.message,
            metadata=self._user_metadata(attachment_metas),
        )
        await self._bind_attachments(
            user_id, request.attachment_ids, user_message_id, conversation_id,
        )

        # 3. 保存 AI 消息（带工具轨迹，供前端刷新后回显工具过程）
        await message_service.create_assistant_message(
            user_id=user_id,
            conversation_id=conversation_id,
            content=final_content,
            metadata={"tools": tool_traces} if tool_traces else None,
        )
        await conversation_service.set_title_if_empty(
            conversation_id,
            user_id,
            request.message.strip().replace("\n", " ")[:40] or "新对话",
        )

        # 3.5 计费埋点（本轮真实产出后）
        await self._record_usage(user_id, tool_traces)

        # 4. 返回（带 run_id，便于前端对这个请求调用 /chat/cancel）
        return ChatResponse(
            content=final_content,
            conversation_id=conversation_id,
            run_id=token.run_id,
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
        # 工具轨迹（按 tool_call_id 保序）：tool.start 时先记入参，
        # tool.result 时补结果，落库到 metadata.tools 供前端刷新后回显。
        # 用 dict 而不是 list，是为了让「先 start 后 result」两次事件能合并到同一条。
        tool_traces: dict[str, dict] = {}

        # 取消令牌：本次流式 run 的标识，前端「停止生成」时凭 run_id 通知服务端
        token = create_token(user_id)
        run_id = token.run_id
        stopped = False

        # ====== 读 Redis 上下文 ======
        summary, history = await self._load_context(conversation_id)

        # ====== 读取端：检索长期记忆注入上下文 ======
        memories = await self._retrieve_memories(user_id, request.message)

        # ====== 用户画像：跨会话常驻事实（无条件注入）======
        profile = await self._load_profile(user_id)

        # ====== 附件（P2 多模态）======
        attachment_text, image_parts, attachment_metas = (
            await self._prepare_attachments(user_id, request.attachment_ids)
        )

        # ====== 头部事件 ======
        yield sse(
            "agent.start",
            {"conversation_id": conversation_id, "run_id": run_id},
        )

        yield sse(
            "message.start",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "role": "assistant",
            },
        )

        # ====== Agent 事件循环（外层 try/finally 保证 run token 一定被清理）======
        # 注意：message.start 在上面已经发过一次（紧跟 agent.start），
        # 这里**不能再发** —— 否则前端一次对话收到两条同 message_id 的
        # message.start，按此事件初始化卡片会导致重复渲染 / 状态重置。
        try:
            try:
                async for event in self.agent.stream(
                    message=request.message,
                    history=history,
                    summary=summary,
                    memories=memories,
                    profile=profile,
                    cancel_token=token,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    attachment_text=attachment_text,
                    image_parts=image_parts or None,
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
                        call_id = event.get("tool_call_id") or ""
                        raw_args = event.get("arguments") or ""
                        tool_traces[call_id] = {
                            "id": call_id,
                            "name": event.get("tool_name") or "tool",
                            "arguments": (
                                raw_args
                                if isinstance(raw_args, str)
                                else json.dumps(raw_args, ensure_ascii=False)
                            ),
                            "result": "",
                            "status": "running",
                        }
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
                        call_id = event.get("tool_call_id") or ""
                        trace = tool_traces.get(call_id)
                        if trace is None:
                            trace = {
                                "id": call_id,
                                "name": event.get("tool_name") or "tool",
                                "arguments": "",
                            }
                            tool_traces[call_id] = trace
                        trace["name"] = event.get("tool_name") or trace["name"]
                        trace["result"] = event.get("result") or ""
                        trace["status"] = "done"
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

            except AgentCancelled:
                # 用户主动停止：结束生成，但走正常尾部事件 + 落库（保存已生成的部分）
                stopped = True
                logger.info(
                    "Agent stream cancelled by user: conv=%s run=%s",
                    conversation_id, run_id,
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

            # ====== 消息落库（无论是否取消都尽量保存，保证历史一致）======
            # 放在发尾部事件之前：即使客户端已断开（abort 后连接关闭），
            # 历史也已先落库，避免「点了停止却什么都没存」。
            try:
                user_message_id = await message_service.create_user_message(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    content=request.message,
                    metadata=self._user_metadata(attachment_metas),
                )
                await self._bind_attachments(
                    user_id, request.attachment_ids,
                    user_message_id, conversation_id,
                )
                if final_content.strip():
                    # 落库前把轨迹定稿：未完成的（被取消的）统一标 done，
                    # 否则前端回显会永远停在「调用中…」
                    traces = []
                    for t in tool_traces.values():
                        traces.append(
                            {
                                **t,
                                "status": "done" if t.get("status") != "failed" else "failed",
                            }
                        )
                    persist_content = enforce_grounding(final_content, traces)
                    await message_service.create_assistant_message(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        content=persist_content,
                        metadata={"tools": traces} if traces else None,
                    )
                    if persist_content != final_content:
                        # 按 rstrip 后的长度切片：enforce 会先 rstrip 正文再拼接附注，
                        # 若直接用 len(final_content)，当正文尾部有空白时会切错位置，
                        # 把一小段已有正文当成新增内容重复补发给前端。
                        extra = persist_content[len(final_content.rstrip()):]
                        if extra:
                            yield sse(
                                "message.delta",
                                {
                                    "conversation_id": conversation_id,
                                    "message_id": assistant_message_id,
                                    "content": extra,
                                },
                            )
                        final_content = persist_content
                await conversation_service.set_title_if_empty(
                    conversation_id,
                    user_id,
                    request.message.strip().replace("\n", " ")[:40] or "新对话",
                )
            except Exception:
                logger.exception("Failed to persist stream messages")

            # ====== 计费埋点（本轮有真实产出后；用户主动停止也算已服务）======
            await self._record_usage(
                user_id, list(tool_traces.values())
            )

            # ====== 尾部事件 ======
            try:
                yield sse(
                    "message.end",
                    {
                        "conversation_id": conversation_id,
                        "message_id": assistant_message_id,
                        "finish_reason": "cancelled" if stopped else "stop",
                    },
                )
                yield sse(
                    "agent.end",
                    {"conversation_id": conversation_id},
                )
                yield sse(
                    "done",
                    {"conversation_id": conversation_id},
                )
            except Exception:
                # 客户端可能已断开（点了停止并 abort），尾部事件发不出去属正常
                logger.info(
                    "Tail SSE not fully sent (client may have disconnected): conv=%s",
                    conversation_id,
                )
        finally:
            remove_token(run_id)


chat_service = ChatService()
