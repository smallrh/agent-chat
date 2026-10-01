import logging

from openai import OpenAI

from app.core.config import settings
from app.tool.registry import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)


def _format_messages_for_log(messages: list) -> str:
    """把 OpenAI messages list 格式化成一行日志预览，截断过长 content。"""
    lines = []
    for i, m in enumerate(messages):
        role = m.get("role", "?") if isinstance(m, dict) else "?"
        content = m.get("content", "") if isinstance(m, dict) else ""
        if content is None:
            content = ""
        if len(content) > 200:
            content = content[:200] + "..."
        # tool_calls 单独标一下
        tc = m.get("tool_calls") if isinstance(m, dict) else None
        tc_tag = f" [tool_calls={len(tc)}]" if tc else ""
        lines.append(f"  [{i}] {role}{tc_tag}: {content!r}")
    return "\n".join(lines)


class LLM:

    def __init__(self):
        self.client = OpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        )

    def chat(
        self,
        messages: list,
        stream: bool = False,
    ):
        logger.debug(
            "========== LLM.chat() CALL ==========\n"
            "model=%s stream=%s max_tokens=%d temperature=%s tools=%d\n"
            "----- messages -----\n%s\n"
            "======================================",
            settings.OPENAI_MODEL, stream,
            settings.MAX_TOKENS, settings.TEMPERATURE,
            len(TOOL_DEFINITIONS),
            _format_messages_for_log(messages),
        )

        return self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
            max_tokens=settings.MAX_TOKENS,
            temperature=settings.TEMPERATURE,
            stream=stream,
        )


llm = LLM()