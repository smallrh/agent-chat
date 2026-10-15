"""工具执行上下文（contextvar）。

工具（尤其是未来需要用户身份的 search_memory 之类）常常需要当前请求的
user_id / conversation_id，但这些信息不该由 LLM 在 arguments 里"猜"出来。

我们用 contextvars 在「一次请求 → 该次所有工具调用」范围内注入 ToolContext，
工具内部通过 `get_tool_context()` 读取，调用方（Agent）只需要在执行工具前
`tool_executor.execute(..., user_id=...)` 即可，工具函数签名保持干净。

用 contextvar 的好处：
- 不污染工具函数签名（weather(city, time_range) 完全不用改）
- 天然跟着「协程 / 线程」走，多用户并发互不串台
- 工具执行是同步还是异步都无所谓，context 都在
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class ToolContext:
    user_id: int | None = None
    conversation_id: str | None = None
    # 主事件循环引用：异步工具（如 search_memory）通过它桥接回主循环执行，
    # 从而复用绑定在主循环上的 Qdrant / PG / 检索单例，避免跨 loop 报错。
    loop: "asyncio.AbstractEventLoop | None" = None
    # 链路追踪 ID。线程池不继承 contextvars，只能靠字段传过去再恢复，
    # 否则工具内部和它打出的日志都拿不到 trace_id。
    trace_id: str | None = None


_TOOL_CONTEXT: ContextVar[ToolContext] = ContextVar(
    "tool_context", default=ToolContext()
)


def get_tool_context() -> ToolContext:
    """工具内部调用，拿到当前请求的 user_id / conversation_id。"""
    return _TOOL_CONTEXT.get()


class tool_context_scope:
    """上下文管理器：进入工具执行时设置，退出时复原（避免串台）。"""

    def __init__(self, ctx: ToolContext):
        self._ctx = ctx
        self._token = None

    def __enter__(self) -> ToolContext:
        self._token = _TOOL_CONTEXT.set(self._ctx)
        return self._ctx

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            _TOOL_CONTEXT.reset(self._token)
