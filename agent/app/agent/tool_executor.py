"""工具执行器：参数装配 + 交给 ToolGuard 安全执行。

职责划分：
    本文件   负责「把 LLM 给的 JSON 参数装配成函数调用参数」
    guard    负责「怎么安全地跑」（超时 / 重试 / 熔断 / 配额 / 审计）

## 两个入口

    execute(...)   同步，供 Agent.chat（非流式）使用 —— 它本身跑在 to_thread 里，
                   阻塞等待即可
    aexecute(...)  异步，供 Agent.stream（流式）使用 —— 直接 await，
                   等待期间让出事件循环，**不额外占用 to_thread 线程**

这一点很重要：如果流式路径也套一层 `asyncio.to_thread(execute)`，
每个工具调用会占用 2 个线程（一层 to_thread 阻塞 + 一层 guard 线程池），
多工具并发时线程池消耗直接翻倍。

## 执行环境

工具执行跑在 worker 线程里（Agent 用 to_thread 包了一层），因此：
  - 同步工具 → 丢进 guard 的线程池执行，`future.result(timeout)` 控时
  - 异步工具 → 通过 `run_coroutine_threadsafe` 桥接回**主事件循环**执行，
    这样能复用绑定在主循环上的 Qdrant / PG / 检索单例
    （search_memory 依赖这条路径）
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging

from app.core.obs import get_trace_id
from app.tool.context import ToolContext, tool_context_scope
from app.tool.guard import tool_guard
from app.tool.registry import get_meta
from app.tool.spec import ToolMeta

logger = logging.getLogger(__name__)


class ToolExecutor:

    # ------------------------------------------------------------------
    # 对外：同步入口
    # ------------------------------------------------------------------

    def execute(
        self,
        tool_name: str,
        arguments: str,
        *,
        user_id: int | None = None,
        conversation_id: str | None = None,
        loop: "asyncio.AbstractEventLoop | None" = None,
    ) -> str:
        """同步执行（调用方若在事件循环里，应改用 aexecute）。"""

        ctx = ToolContext(
            user_id=user_id,
            conversation_id=conversation_id,
            loop=loop,
            # 显式带上：工具在线程池/主循环里执行时才能把这个 trace 续上
            trace_id=get_trace_id(),
        )

        with tool_context_scope(ctx):
            return self._execute(tool_name, arguments, ctx)

    # ------------------------------------------------------------------
    # 对外：异步入口
    # ------------------------------------------------------------------

    async def aexecute(
        self,
        tool_name: str,
        arguments: str,
        *,
        user_id: int | None = None,
        conversation_id: str | None = None,
        loop: "asyncio.AbstractEventLoop | None" = None,
    ) -> str:
        """异步执行：同步工具丢线程池并 await，不阻塞事件循环。"""

        ctx = ToolContext(
            user_id=user_id,
            conversation_id=conversation_id,
            loop=asyncio.get_running_loop(),
            trace_id=get_trace_id(),
        )

        with tool_context_scope(ctx):
            return await self._aexecute(tool_name, arguments, ctx)

    # ------------------------------------------------------------------
    # 内部：参数装配（两条路径共用，保证语义一致）
    # ------------------------------------------------------------------

    def _prepare(
        self,
        tool_name: str,
        arguments: str,
        ctx: ToolContext,
    ) -> tuple[ToolMeta | None, dict[str, Any] | None, str | None]:
        """解析并装配参数。

        Returns:
            (meta, call_kwargs, error)：error 非空时应该直接返回给 LLM。
        """
        try:
            tool_args = json.loads(arguments)
        except json.JSONDecodeError:
            return None, None, "工具参数解析失败：arguments 不是合法 JSON"

        if not isinstance(tool_args, dict):
            return None, None, "工具参数必须是 JSON 对象"

        meta = get_meta(tool_name)
        if meta is None:
            from app.tool.registry import TOOLS

            available = ", ".join(sorted(TOOLS)) or "（当前无可用工具）"
            return None, None, f"工具不存在：{tool_name}。可用工具：{available}"

        try:
            call_kwargs = self._build_kwargs(meta, tool_args, ctx)
        except TypeError as e:
            return None, None, f"工具参数错误：{e}"

        return meta, call_kwargs, None

    def _execute(self, tool_name: str, arguments: str, ctx: ToolContext) -> str:
        meta, call_kwargs, error = self._prepare(tool_name, arguments, ctx)
        if error:
            return error
        return tool_guard.run(meta, call_kwargs, ctx).text

    async def _aexecute(
        self,
        tool_name: str,
        arguments: str,
        ctx: ToolContext,
    ) -> str:
        meta, call_kwargs, error = self._prepare(tool_name, arguments, ctx)
        if error:
            return error
        result = await tool_guard.arun(meta, call_kwargs, ctx)
        return result.text

    # ------------------------------------------------------------------
    # 参数装配
    # ------------------------------------------------------------------

    @staticmethod
    def _build_kwargs(
        meta: ToolMeta,
        tool_args: dict,
        ctx: ToolContext,
    ) -> dict:
        """按函数签名把 LLM 参数装配成调用 kwargs。

        三类来源：
          1. LLM 传入的正常参数
          2. user_id / conversation_id —— 由 ToolContext 注入（工具声明了才注入）
          3. 有默认值的参数 —— 不传就用默认值

        LLM 多传的参数一律忽略（不进 kwargs），避免模型幻觉字段炸掉调用。
        """
        sig = inspect.signature(meta.impl)
        call_kwargs: dict = {}
        for name in sig.parameters:
            if name in tool_args:
                call_kwargs[name] = tool_args[name]
            elif name == "user_id":
                call_kwargs["user_id"] = ctx.user_id
            elif name == "conversation_id":
                call_kwargs["conversation_id"] = ctx.conversation_id
        return call_kwargs


tool_executor = ToolExecutor()
