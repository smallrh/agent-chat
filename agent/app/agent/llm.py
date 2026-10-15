import logging

from openai import OpenAI

import time

from app.core.config import settings
from app.core.obs import (
    M_LLM_CALLS,
    M_LLM_DURATION,
    M_LLM_TOKENS,
    metrics,
)
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
            # 客户端级兜底超时：任何一次调用都不会无限挂着占住 worker 线程。
            # 单次请求还可以在 chat() 里再传 timeout 覆盖。
            timeout=float(settings.LLM_TIMEOUT_SECONDS),
        )

    def chat(
        self,
        messages: list,
        stream: bool = False,
        timeout: float | None = None,
    ):
        """
        调用 Chat Completions。

        timeout 单位为秒；不传则用 settings.LLM_TIMEOUT_SECONDS。
        超时会抛 openai.APITimeoutError，由调用方决定降级还是报错 ——
        关键是**不能让模型无限卡住**（非流式 /chat 尤其危险：它会一直占住
        worker 线程和连接池，而且没有 cancel token 可以救）。
        """
        req_timeout = (
            float(timeout) if timeout else float(settings.LLM_TIMEOUT_SECONDS)
        )

        logger.debug(
            "========== LLM.chat() CALL ==========\n"
            "model=%s stream=%s max_tokens=%d temperature=%s tools=%d timeout=%s\n"
            "----- messages -----\n%s\n"
            "======================================",
            settings.OPENAI_MODEL, stream,
            settings.MAX_TOKENS, settings.TEMPERATURE,
            len(TOOL_DEFINITIONS), req_timeout,
            _format_messages_for_log(messages),
        )

        started = time.perf_counter()
        try:
            resp = self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                max_tokens=settings.MAX_TOKENS,
                temperature=settings.TEMPERATURE,
                stream=stream,
                timeout=req_timeout,
            )
        except Exception:
            metrics.inc(M_LLM_CALLS, 1, status="error")
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000
        stream_flag = "true" if stream else "false"
        metrics.inc(M_LLM_CALLS, 1, status="ok", stream=stream_flag)
        metrics.observe(M_LLM_DURATION, elapsed_ms, stream=stream_flag)

        # 流式响应是 Stream 对象，没有 usage；只有非流式能拿到 token 用量
        usage = getattr(resp, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        if isinstance(total_tokens, int):
            metrics.inc(
                M_LLM_TOKENS, total_tokens,
                model=settings.OPENAI_MODEL,
            )
        return resp


llm = LLM()