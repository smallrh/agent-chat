import re

from app.agent.llm import llm
from app.agent.tool_executor import tool_executor
from app.core.config import settings


class Agent:

    SYSTEM_PROMPT = "You are a helpful assistant"

    # 记忆注入块头部：明确告诉模型这是检索到的长期记忆，并约束使用方式
    MEMORY_BLOCK_HEADER = (
        "\n\n=== 用户长期记忆（来自记忆库，仅供参考）===\n"
        "以下是系统根据用户当前问题语义检索到的长期记忆（已按相关度排序）。\n"
        "回答时优先考虑这些记忆中的事实；若记忆与问题无关或缺失，按你的常识正常回答，"
        "不要编造记忆以外的细节。"
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

    # ----------------------------------------------------------
    # 构造 messages：system(+summary+memory) → history → 当前 user
    # ----------------------------------------------------------

    def _build_messages(
        self,
        message: str,
        history: list[dict] | None = None,
        summary: str | None = None,
        memories: list[dict] | None = None,
    ) -> list[dict]:
        """
        拼装发给 LLM 的 messages。

        顺序：system（含 summary 注入 + 记忆注入）→ history（旧对话）→ 当前 user
        history 里的每条 dict 必须是 OpenAI message 格式：{"role": ..., "content": ...}

        memories：读取端检索到的长期记忆（已按 score 降序），注入到 system 中，
        这样无论是第一次 LLM 还是工具调用后的第二次 LLM 都能看到。
        """
        system_content = self.SYSTEM_PROMPT
        if summary:
            system_content += f"\n\n=== 对话历史摘要 ===\n{summary}"

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

        messages.append({"role": "user", "content": message})
        return messages

    # ----------------------------------------------------------
    # 非流式
    # ----------------------------------------------------------

    def chat(
        self,
        message: str,
        history: list[dict] | None = None,
        summary: str | None = None,
        memories: list[dict] | None = None,
    ) -> dict:

        messages = self._build_messages(
            message=message,
            history=history,
            summary=summary,
            memories=memories,
        )

        # =========================
        # 1. 第一次 LLM
        # =========================

        response = llm.chat(
            messages=messages,
            stream=False,
        )

        assistant_message = response.choices[0].message

        # =========================
        # 2. Tool Calling
        # =========================

        if assistant_message.tool_calls:

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_message.content,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            },
                        }
                        for tool_call in assistant_message.tool_calls
                    ],
                }
            )

            # 执行所有工具
            for tool_call in assistant_message.tool_calls:

                tool_result = tool_executor.execute(
                    tool_name=tool_call.function.name,
                    arguments=tool_call.function.arguments,
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    }
                )

            # =========================
            # 3. 第二次 LLM
            # =========================

            final_response = llm.chat(
                messages=messages,
                stream=False,
            )

            final_content = (
                final_response.choices[0].message.content
                or ""
            )

        else:

            final_content = (
                assistant_message.content
                or ""
            )

        return {
            "content": final_content,
            "messages": messages,
        }

    async def stream(
        self,
        message: str,
        history: list[dict] | None = None,
        summary: str | None = None,
        memories: list[dict] | None = None,
    ):
        """
        Agent 流式执行，产出原始事件流（不带 conversation_id / message_id 等信封，
        信封由上层 ChatService 统一补上）。

        产出事件类型：
          - message.delta   每段文本增量
          - tool.start      某个工具开始执行
          - tool.result      某个工具返回
          - tool.end        某个工具执行结束
          - agent.end       Agent 内部结束（不再发 done，done 由 ChatService 统一发）
        """

        messages = self._build_messages(
            message=message,
            history=history,
            summary=summary,
            memories=memories,
        )

        # =========================
        # 1. 第一次 LLM
        # =========================

        response = llm.chat(
            messages=messages,
            stream=True,
        )

        content_parts = []

        tool_calls = {}

        # =========================
        # 2. 流式读取
        # =========================

        for chunk in response:

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # 普通文本
            if delta.content:

                content_parts.append(
                    delta.content
                )

                yield {
                    "type": "message.delta",
                    "content": delta.content,
                }

            # Tool Call
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
                        tool_calls[index]["id"] = (
                            tool_call.id
                        )

                    if tool_call.function:

                        if tool_call.function.name:
                            tool_calls[index]["name"] = (
                                tool_call.function.name
                            )

                        if tool_call.function.arguments:
                            tool_calls[index]["arguments"] += (
                                tool_call.function.arguments
                            )

        # =========================
        # 3. 没有 Tool，Agent 直接结束
        # =========================

        if not tool_calls:

            yield {
                "type": "agent.end",
            }

            return

        # =========================
        # 4. 构建 Tool Calls
        # =========================

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

        messages.append(
            {
                "role": "assistant",
                "content": (
                    "".join(content_parts)
                    if content_parts
                    else None
                ),
                "tool_calls": assistant_tool_calls,
            }
        )

        # =========================
        # 5. 逐个执行 Tool
        # =========================

        for item in tool_calls.values():

            tool_call_id = item["id"]
            tool_name = item["name"]

            yield {
                "type": "tool.start",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": item["arguments"],
            }

            tool_result = tool_executor.execute(
                tool_name=tool_name,
                arguments=item["arguments"],
            )

            yield {
                "type": "tool.result",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "result": tool_result,
            }

            yield {
                "type": "tool.end",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
            }

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": tool_result,
                }
            )

        # =========================
        # 6. 第二次 LLM（带工具结果）
        # =========================

        final_response = llm.chat(
            messages=messages,
            stream=True,
        )

        for chunk in final_response:

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            if delta.content:

                yield {
                    "type": "message.delta",
                    "content": delta.content,
                }

        # =========================
        # 7. Agent 内部结束
        # =========================

        yield {
            "type": "agent.end",
        }


agent = Agent()