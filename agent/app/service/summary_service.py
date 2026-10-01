import logging

from openai import AsyncOpenAI

from app.core.config import settings
from app.repo.redis_repo import RedisRepository

logger = logging.getLogger(__name__)


_SUMMARY_SYSTEM_PROMPT = """你是一个对话摘要助手。把给定的对话历史压缩成一段自然语言摘要，
用于在下一轮对话时给模型提供上下文锚点。

要求：
  1. 200 字以内，保留核心事实和关键动作，丢弃寒暄、重复和枝节
  2. 以第三人称视角概述，不要逐句复述
  3. 如果已有旧摘要，在其基础上增量更新（旧的不重复，只补新进展）
  4. 只输出摘要正文，不要加"以下是摘要"之类的前缀"""


_SUMMARY_USER_TEMPLATE = """已有旧摘要（可为空）：
{existing_summary}

以下是本轮对话消息：
{messages_text}

请输出更新后的完整摘要（200 字以内）。"""


def _format_messages(messages: list[dict]) -> str:
    """把 message list 格式化成 LLM 可读的文本，截断过长内容。"""
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if len(content) > 300:
            content = content[:300] + "..."
        lines.append(f"[{role}]: {content}")
    return "\n".join(lines)


class SummaryService:
    """
    对话摘要生成 + 读取。

    当对话积累一定量消息后，把最近 20 条压缩成一段摘要放回 Redis，
    这样后续 LLM 调用时不用把全部历史消息塞进去。

    generate() 内部用 AsyncOpenAI 调 LLM（和 MemoryWorker 里的
    MemoryService 使用同一套 API Key / Base URL）。
    """

    def __init__(self, redis_repo: RedisRepository | None = None):
        self.redis_repo = redis_repo
        # 内部共享 AsyncOpenAI client（和 MemoryService 一样）
        self._llm = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        )

    # --------------------------------------------------------------
    # 生成
    # --------------------------------------------------------------

    async def generate(
        self,
        messages: list[dict],
        existing_summary: str | None = None,
    ) -> str | None:
        """
        把一组 messages 压缩成一段自然语言摘要。

        - 有 existing_summary → 增量更新（prompt 里带上旧摘要）
        - LLM 不可用 / 响应为空 → 返回 None（上层跳过写 Redis，下次重试）
        """
        if not messages:
            return None

        formatted = _format_messages(messages)
        existing = existing_summary or ""

        call_messages = [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _SUMMARY_USER_TEMPLATE.format(
                    existing_summary=existing,
                    messages_text=formatted,
                ),
            },
        ]

        logger.debug(
            "========== SummaryService.generate() LLM CALL ==========\n"
            "model=%s max_tokens=400 temperature=0.2\n"
            "existing_summary=%s new_messages=%d\n"
            "----- messages -----\n%s\n"
            "=============================================",
            settings.OPENAI_MODEL,
            bool(existing), len(messages),
            "\n".join(
                f"  [{i}] {m['role']}: {m['content'][:300]!r}{'...' if len(m['content']) > 300 else ''}"
                for i, m in enumerate(call_messages)
            ),
        )

        try:
            response = await self._llm.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=call_messages,
                max_tokens=400,
                temperature=0.2,
            )
        except Exception as exc:
            logger.exception("SummaryService.generate LLM call FAILED: %s", exc)
            return None

        summary = (
            response.choices[0].message.content or ""
        ).strip()

        if not summary:
            logger.warning("SummaryService.generate: LLM returned empty content")
            return None

        logger.info(
            "SummaryService.generate: len=%d (existing=%s, new_len=%d)",
            len(summary),
            bool(existing),
            len(messages),
        )
        return summary
