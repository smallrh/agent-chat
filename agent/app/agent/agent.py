import asyncio
import logging
import re

from app.agent.cancel import AgentCancelled, RunToken
from app.agent.llm import llm
from app.agent.tool_executor import tool_executor
from app.core.config import settings
from app.service.grounding import reminder_for

logger = logging.getLogger(__name__)

_STREAM_END = object()


def _sync_next(it):
    try:
        return next(it)
    except StopIteration:
        return _STREAM_END


def _search_limit_message(limit: int) -> str:
    """联网搜索达到单轮上限时，返回给模型的替代「工具结果」。"""
    return (
        f"[系统限制] 本轮联网搜索次数已达上限（{limit} 次），本次不再发起搜索。"
        "请基于上面已获得的搜索结果作答；若资料确实不足，直接说明"
        "「检索结果不足以确认」，禁止编造地址、评分、价格等具体事实。"
    )


class _SearchBudget:
    """单轮对话内 web_search 的调用次数预算（默认允许 1 次失败后改写重试）。

    目的：堵住「第一次搜到了标题和 URL，模型还要换个近义词再搜一遍」这类
    最常见也最费的循环。0 = 不限。
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def take(self, tool_name: str | None) -> str | None:
        """申请一次调用额度。

        Returns:
            None  = 放行；否则返回要直接当作工具结果的限制提示。
        """
        if tool_name != "web_search" or self.limit <= 0:
            return None
        if self.used >= self.limit:
            return _search_limit_message(self.limit)
        self.used += 1
        return None


def _append_stopped(content: str | None) -> str:
    """给「被取消」的半截内容补一句提示（内容为空时只返回提示语）。"""
    text = (content or "").rstrip()
    return f"{text}\n\n（已停止生成）" if text else "（已停止生成）"


class Agent:

    SYSTEM_PROMPT = (
        "你是一个会调用工具的中文助手。"
        "回答事实类问题（地点、酒店、公司、价格、地址、评分、营业信息等）时："
        "1) 必须先用工具拿到资料，再作答；同一问题默认只调用一次 web_search；"
        "2) 第一次搜索只要返回了标题和 URL，就必须立刻根据这些结果作答，禁止换个近义词再搜一遍；"
        "3) 只有工具明确失败、超时或「未检索到相关结果」时，才允许改写关键词再搜一次；"
        "4) 只能陈述工具返回里出现的事实，禁止用训练记忆补地址、评分、地铁站、周边商户；"
        "5) 每条关键信息后面附上对应来源 URL；"
        "6) 结果指向多家实体时，列出候选并请用户确认，不要合成一家不存在的店；"
        "7) 摘要不足以确认时，明确说「检索结果不足以确认」，不要编造。"
        "工具结果里没有的细节一律不要写。"
    )

    # 记忆注入块头部：明确告诉模型这是检索到的长期记忆，并约束使用方式
    MEMORY_BLOCK_HEADER = (
        "\n\n=== 用户长期记忆（来自记忆库，仅供参考）===\n"
        "以下是系统根据用户当前问题语义检索到的长期记忆（已按相关度排序）。\n"
        "回答时优先考虑这些记忆中的事实；若记忆与问题无关或缺失，按你的常识正常回答，"
        "不要编造记忆以外的细节。"
    )

    # 常见 slot 的中文标签（仅用于注入展示，降低模型把相近槽位搞混的概率）
    _SLOT_LABELS: dict[str, str] = {
        "hometown": "籍贯/老家",
        "residence_city": "现居地/工作地",
        "name": "姓名",
        "job_title": "职位",
        "employer": "公司",
        "school": "学校",
        "favorite_food": "喜欢的食物",
        "favorite_sport": "喜欢的运动",
        "favorite_music": "喜欢的音乐",
        "primary_language": "主要编程语言",
        "birthday": "生日",
        "contact": "联系方式",
    }

    # 用户画像块头部：跨会话常驻的"已知用户事实"（不依赖语义检索是否命中）
    PROFILE_BLOCK_HEADER = (
        "\n\n=== 已知用户基本信息（跨会话长期有效）===\n"
        "以下是用户在过往会话中明确告知过的稳定事实（所属会话可能已结束）。\n"
        "这些事实在任何新会话中都成立；回答涉及用户身份/籍贯/偏好等问题时直接使用，"
        "不要再反问用户，也不要因为当前会话没提过就当作不知道。"
    )

    # ----------------------------------------------------------
    # 记忆块格式化（带来源标记 + 置信度 + 注入上限/token 预算）
    # ----------------------------------------------------------

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """
        中英文混合的粗略 token 估算，用于注入预算（偏保守，避免超预算）：
          - 每个 CJK 汉字 ≈ 1 token
          - 其余字符（英文/标点/空格）≈ 每 4 字符 1 token
        """
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        non_cjk = len(text) - cjk
        return cjk + non_cjk // 4 + 1

    @classmethod
    def _format_memory_block(
        cls,
        memories: list[dict],
        max_items: int,
        token_budget: int,
    ) -> str:
        """
        把检索到的记忆格式化成注入文本。

        规则：
          - 按传入顺序（调用方已按 score 降序）逐条追加
          - 受 max_items 与 token_budget 双重约束，超出即停止
          - 每条带来源标记（memory_id）+ 置信度
        """
        if not memories:
            return ""

        used = cls._estimate_tokens(cls.MEMORY_BLOCK_HEADER)
        lines: list[str] = []

        for m in memories[:max_items]:
            content = (m.get("content") or "").strip()
            if not content:
                continue
            mtype = m.get("memory_type") or "unknown"
            mid = m.get("memory_id") or "?"
            conf = m.get("confidence")
            conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "N/A"
            line = f"- [类型:{mtype} | 来源:{mid} | 置信度:{conf_str}] {content}"

            line_tokens = cls._estimate_tokens(line)
            if used + line_tokens > token_budget:
                # 再加这条会超预算，停止追加（高相关度的已经在前面）
                break
            lines.append(line)
            used += line_tokens

        if not lines:
            return ""
        return cls.MEMORY_BLOCK_HEADER + "\n" + "\n".join(lines)

    @classmethod
    def _format_profile_block(cls, profile: dict) -> str:
        """
        把用户画像格式化成常驻注入块。

        与记忆块的区别：记忆块是"按当前问题语义召回"的（可能召回不到），
        profile 块是"无条件常驻"的——这是跨会话必知事实的兜底通道。
        同一 profile key 只保留最新值，故体量很小，不额外做 token 预算裁剪。
        """
        if not profile:
            return ""

        lines: list[str] = []
        for key, value in profile.items():
            # 跳过内部字段（如 _schema_version）
            if not key or str(key).startswith("_"):
                continue
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            # 已知 slot 给个中文标签，降低模型混淆概率
            # （尤其 hometown=籍贯 vs residence_city=现居地 这两个最容易被搞混）
            label = cls._SLOT_LABELS.get(key)
            lines.append(
                f"- {label}（{key}）: {text}" if label else f"- {key}: {text}"
            )

        if not lines:
            return ""
        return cls.PROFILE_BLOCK_HEADER + "\n" + "\n".join(lines)

    # ----------------------------------------------------------
    # 构造 messages：system(+summary+memory) → history → 当前 user
    # ----------------------------------------------------------

    def _build_messages(
        self,
        message: str,
        history: list[dict] | None = None,
        summary: str | None = None,
        memories: list[dict] | None = None,
        profile: dict | None = None,
        attachment_text: str = "",
        image_parts: list[dict] | None = None,
    ) -> list[dict]:
        """
        拼装发给 LLM 的 messages。

        顺序：system（含 summary 注入 + 用户画像 + 记忆注入）→ history → 当前 user
        history 里的每条 dict 必须是 OpenAI message 格式：{"role": ..., "content": ...}

        附件（P2 多模态）：
          - attachment_text：已拼好的「【附件】name ...」文本块，直接追加到
            用户消息尾部。用户什么都没写（只发了附件）时也能正常工作。
          - image_parts：OpenAI 多模态内容块列表。有图片时整个 user content
            会从「字符串」变成「content part 数组」——这是 vision 模型
            要求的格式，字符串形式传图片 URL 是无效的。
        """
        system_content = self.SYSTEM_PROMPT
        if summary:
            system_content += f"\n\n=== 对话历史摘要 ===\n{summary}"

        # 用户画像：跨会话常驻，无条件注入（不依赖语义召回）
        if profile:
            block = self._format_profile_block(profile)
            if block:
                system_content += block

        if memories:
            block = self._format_memory_block(
                memories,
                settings.MEMORY_INJECT_MAX_ITEMS,
                settings.MEMORY_INJECT_TOKEN_BUDGET,
            )
            if block:
                system_content += block

        messages: list[dict] = [
            {"role": "system", "content": system_content},
        ]

        if history:
            messages.extend(history)

        user_text = message or ""
        if attachment_text:
            user_text = (
                f"{user_text}\n\n{attachment_text}" if user_text else attachment_text
            )

        if image_parts:
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_text}, *image_parts],
                }
            )
        else:
            messages.append({"role": "user", "content": user_text})
        return messages

    # ----------------------------------------------------------
    # 把一次 LLM 响应里的 tool_calls 归并成标准结构
    # ----------------------------------------------------------

    @staticmethod
    def _build_assistant_tool_calls(tool_calls: dict) -> list[dict]:
        """
        把流式累积的 tool_calls（index -> {id,name,arguments}）转成
        OpenAI message 里 assistant.tool_calls 的标准结构。
        """
        assistant_tool_calls = []
        for item in tool_calls.values():
            assistant_tool_calls.append(
                {
                    "id": item["id"],
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": item["arguments"],
                    },
                }
            )
        return assistant_tool_calls

    # ----------------------------------------------------------
    # 非流式（多步工具循环）
    # ----------------------------------------------------------

    def chat(
        self,
        message: str,
        history: list[dict] | None = None,
        summary: str | None = None,
        memories: list[dict] | None = None,
        profile: dict | None = None,
        user_id: int | None = None,
        conversation_id: str | None = None,
        loop: "asyncio.AbstractEventLoop | None" = None,
        cancel_token: object | None = None,
        attachment_text: str = "",
        image_parts: list[dict] | None = None,
    ) -> dict:

        messages = self._build_messages(
            message=message,
            history=history,
            summary=summary,
            memories=memories,
            profile=profile,
            attachment_text=attachment_text,
            image_parts=image_parts,
        )

        final_content = ""
        round_no = 0
        # 工具轨迹：落库到 message.metadata.tools，供前端刷新后回显工具过程
        tool_traces: list[dict] = []
        # 单轮联网搜索次数预算
        search_budget = _SearchBudget(settings.SEARCH_MAX_CALLS_PER_TURN)

        # =========================
        # 多步工具循环（ReAct）
        # 每一轮：调 LLM → 若有 tool_calls 就执行 → 把结果塞回 messages
        #        → 继续下一轮；直到 LLM 不再要工具或达到上限
        # =========================

        while True:
            round_no += 1

            # 协作式取消：每轮开始前看一眼 run token。
            # 非流式跑在 worker 线程里，不能像流式那样靠 asyncio 取消，
            # 只能在检查点主动退出（工具调用越多的场景收益越明显）。
            if cancel_token is not None and getattr(
                cancel_token, "cancelled", False,
            ):
                logger.info(
                    "Agent.chat cancelled at round %d (run_id=%s)",
                    round_no, getattr(cancel_token, "run_id", "?"),
                )
                final_content = _append_stopped(final_content)
                break

            if round_no > settings.MAX_TOOL_ROUNDS:
                final_content = (
                    f"{final_content}\n\n（已达到最大工具调用轮次"
                    f"{settings.MAX_TOOL_ROUNDS}，停止继续调用工具。）"
                )
                break

            response = llm.chat(
                messages=messages,
                stream=False,
            )
            assistant_message = response.choices[0].message

            # 没有工具调用 → 这就是最终答案
            if not assistant_message.tool_calls:
                final_content = assistant_message.content or ""
                break

            # 有工具调用 → 把 assistant(tool_calls) 记录进 messages，
            # 再逐个执行，把 tool 结果也塞回去（OpenAI 要求的格式）
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in assistant_message.tool_calls
                    ],
                }
            )

            for tc in assistant_message.tool_calls:
                # 每个工具执行前再看一次：工具往往是耗时主因
                if cancel_token is not None and getattr(
                    cancel_token, "cancelled", False,
                ):
                    logger.info(
                        "Agent.chat cancelled before tool %s (run_id=%s)",
                        tc.function.name,
                        getattr(cancel_token, "run_id", "?"),
                    )
                    return {"content": _append_stopped(final_content)}

                limit_hint = search_budget.take(tc.function.name)
                if limit_hint is not None:
                    tool_result = limit_hint
                else:
                    tool_result = tool_executor.execute(
                        tool_name=tc.function.name,
                        arguments=tc.function.arguments,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        loop=loop,
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                )
                tool_traces.append(
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                        "result": tool_result,
                        "status": "done",
                    }
                )

                # grounding：搜索没拿到任何可用条目时，紧跟证据补一条硬提醒，
                # 比 system prompt 里的软约束更靠近证据，能显著压住「脑补地址/评分」。
                hint = reminder_for(tc.function.name, tool_result)
                if hint:
                    messages.append({"role": "system", "content": hint})

        return {
            "content": final_content,
            "messages": messages,
            "tool_traces": tool_traces,
        }

    # ----------------------------------------------------------
    # 流式（多步工具循环）
    # ----------------------------------------------------------

    async def stream(
        self,
        message: str,
        history: list[dict] | None = None,
        summary: str | None = None,
        memories: list[dict] | None = None,
        profile: dict | None = None,
        cancel_token: "RunToken | None" = None,
        user_id: int | None = None,
        conversation_id: str | None = None,
        attachment_text: str = "",
        image_parts: list[dict] | None = None,
    ):
        """
        Agent 流式执行（多步工具循环），产出原始事件流（不带 conversation_id /
        message_id 等信封，信封由上层 ChatService 统一补上）。

        产出事件类型：
          - message.delta   每段文本增量（每个工具轮次的文本都会被流式吐出）
          - tool.start      某个工具开始执行
          - tool.result      某个工具返回
          - tool.end        某个工具执行结束
          - agent.end       Agent 内部结束（done 由 ChatService 统一发）

        多步逻辑：
          每一轮先流式读 LLM；若本轮产出了 tool_calls → 逐个执行工具并把结果
          塞回 messages，然后「再调一轮 LLM」；直到某轮 LLM 不再请求工具或
          达到 MAX_TOOL_ROUNDS 上限。这样工具之间可以链式依赖
          （例如 weather 拿到温度 → calculator 比较大小）。
        """

        messages = self._build_messages(
            message=message,
            history=history,
            summary=summary,
            memories=memories,
            profile=profile,
            attachment_text=attachment_text,
            image_parts=image_parts,
        )

        round_no = 0
        # 单轮联网搜索次数预算（与非流式同一套策略）
        search_budget = _SearchBudget(settings.SEARCH_MAX_CALLS_PER_TURN)

        while True:
            round_no += 1
            if cancel_token is not None and cancel_token.cancelled:
                raise AgentCancelled()

            if round_no > settings.MAX_TOOL_ROUNDS:
                yield {
                    "type": "message.delta",
                    "content": (
                        f"\n\n（已达到最大工具调用轮次"
                        f"{settings.MAX_TOOL_ROUNDS}，停止继续调用工具。）"
                    ),
                }
                break

            # =========================
            # 本轮：调一次 LLM（流式），边读边吐文本，同时收集 tool_calls
            # =========================

            response = await asyncio.to_thread(
                llm.chat,
                messages,
                True,
            )

            content_parts = []
            tool_calls: dict = {}
            stream_iter = iter(response)

            while True:
                if cancel_token is not None and cancel_token.cancelled:
                    raise AgentCancelled()

                chunk = await asyncio.to_thread(_sync_next, stream_iter)
                if chunk is _STREAM_END:
                    break

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # 普通文本：流式吐出（每个工具轮次的前言/最终答案都走这里）
                if delta.content:
                    content_parts.append(delta.content)
                    yield {
                        "type": "message.delta",
                        "content": delta.content,
                    }

                # Tool Call：流式累积（arguments 可能分多个 chunk 到达）
                if delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        index = tool_call.index

                        if index not in tool_calls:
                            tool_calls[index] = {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            }

                        if tool_call.id:
                            tool_calls[index]["id"] = tool_call.id

                        if tool_call.function:
                            if tool_call.function.name:
                                tool_calls[index]["name"] = tool_call.function.name
                            if tool_call.function.arguments:
                                tool_calls[index]["arguments"] += (
                                    tool_call.function.arguments
                                )

            # =========================
            # 本轮没有 tool_calls → 最终答案已流完，结束
            # =========================

            if not tool_calls:
                break

            # =========================
            # 把 assistant(tool_calls) 记录进 messages（OpenAI 必需）
            # =========================

            messages.append(
                {
                    "role": "assistant",
                    "content": (
                        "".join(content_parts)
                        if content_parts
                        else None
                    ),
                    "tool_calls": self._build_assistant_tool_calls(tool_calls),
                }
            )

            # =========================
            # 执行本轮的工具调用（无依赖时可以并发），结果塞回 messages
            #
            # 并发策略：
            #   - 只有「本轮有多个 tool_calls」且开关打开、数量未超上限时才并发；
            #     单工具调用走原路径，零额外开销。
            #   - 事件顺序：先一次性发出全部 tool.start（让前端同时点亮所有卡片），
            #     拿到结果后**按 tool_calls 原始顺序**逐条发 tool.result/tool.end，
            #     保证 tool_call_id 与结果一一对应、且 messages 顺序稳定
            #     （OpenAI 要求 tool message 与 assistant.tool_calls 对应）。
            #   - 工具内部的超时/重试/熔断/配额由 ToolGuard 各自负责，
            #     并发不会绕过任何一层保护。
            # =========================

            items = list(tool_calls.values())

            if cancel_token is not None and cancel_token.cancelled:
                raise AgentCancelled()

            for item in items:
                yield {
                    "type": "tool.start",
                    "tool_call_id": item["id"],
                    "tool_name": item["name"],
                    "arguments": item["arguments"],
                }

            parallel = (
                settings.TOOL_PARALLEL_ENABLED
                and len(items) > 1
                and len(items) <= settings.TOOL_MAX_PARALLEL
            )

            results: list[str | None] = [None] * len(items)

            async def _run_one(idx: int, it: dict) -> None:
                if cancel_token is not None and cancel_token.cancelled:
                    results[idx] = "已取消"
                    return
                limit_hint = search_budget.take(it["name"])
                if limit_hint is not None:
                    # 搜索次数用尽：不真的发请求，直接把限制提示当作工具结果
                    results[idx] = limit_hint
                    return
                try:
                    results[idx] = await tool_executor.aexecute(
                        it["name"],
                        it["arguments"],
                        user_id=user_id,
                        conversation_id=conversation_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    results[idx] = f"工具执行失败：{exc}"

            if parallel:
                pending = {
                    asyncio.create_task(_run_one(i, it)): i
                    for i, it in enumerate(items)
                }
                remaining = set(pending)
                while remaining:
                    done, remaining = await asyncio.wait(
                        remaining, return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        idx = pending[task]
                        item = items[idx]
                        tool_result = results[idx] or ""
                        yield {
                            "type": "tool.result",
                            "tool_call_id": item["id"],
                            "tool_name": item["name"],
                            "result": tool_result,
                        }
                        yield {
                            "type": "tool.end",
                            "tool_call_id": item["id"],
                            "tool_name": item["name"],
                        }
            else:
                for idx, it in enumerate(items):
                    if cancel_token is not None and cancel_token.cancelled:
                        raise AgentCancelled()
                    await _run_one(idx, it)
                    tool_result = results[idx] or ""
                    yield {
                        "type": "tool.result",
                        "tool_call_id": it["id"],
                        "tool_name": it["name"],
                        "result": tool_result,
                    }
                    yield {
                        "type": "tool.end",
                        "tool_call_id": it["id"],
                        "tool_name": it["name"],
                    }

            for item, tool_result in zip(items, results):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["id"],
                        "content": tool_result or "",
                    }
                )
                # 同上：搜索无证据时立刻补提醒，避免下一轮继续编造
                hint = reminder_for(item.get("name"), tool_result)
                if hint:
                    messages.append({"role": "system", "content": hint})

        # =========================
        # Agent 内部结束
        # =========================

        yield {
            "type": "agent.end",
        }


agent = Agent()
