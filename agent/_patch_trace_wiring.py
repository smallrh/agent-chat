"""接线：trace_id 贯穿工具链路 + 工具指标埋点。

改动点：
1. ToolContext 增加 trace_id 字段（ThreadPool / 主事件循环都不继承 contextvars，
   必须靠参数显式传过去，再在目标线程里恢复）
2. ToolGuard._sync_call / _async_call 内恢复 trace scope
3. ToolGuard.run 记录 tool_calls_total / tool_duration_ms
4. ToolExecutor 构造 ToolContext 时带上当前 trace_id
"""

import io

# ---------------------------------------------------------------- context.py
p = "app/tool/context.py"
s = io.open(p, encoding="utf-8").read()
old_ctx = '''    # 主事件循环引用：异步工具（如 search_memory）通过它桥接回主循环执行，
    # 从而复用绑定在主循环上的 Qdrant / PG / 检索单例，避免跨 loop 报错。
    loop: "asyncio.AbstractEventLoop | None" = None
'''
new_ctx = '''    # 主事件循环引用：异步工具（如 search_memory）通过它桥接回主循环执行，
    # 从而复用绑定在主循环上的 Qdrant / PG / 检索单例，避免跨 loop 报错。
    loop: "asyncio.AbstractEventLoop | None" = None
    # 链路追踪 ID。线程池不继承 contextvars，只能靠字段传过去再恢复，
    # 否则工具内部和它打出的日志都拿不到 trace_id。
    trace_id: str | None = None
'''
assert old_ctx in s, "context anchor not found"
if "trace_id" not in s:
    s = s.replace(old_ctx, new_ctx)
    io.open(p, "w", encoding="utf-8").write(s)
    print("[ok] context.py + trace_id")
else:
    print("[skip] context.py already has trace_id")

# ---------------------------------------------------------------- guard.py
p = "app/tool/guard.py"
s = io.open(p, encoding="utf-8").read()

if "from app.core.obs import" not in s:
    s = s.replace(
        "from app.core.config import settings\nfrom app.tool.context import ToolContext, tool_context_scope\n",
        "from app.core.config import settings\n"
        "from app.core.obs import (\n"
        "    M_TOOL_CALLS,\n"
        "    M_TOOL_DURATION,\n"
        "    metrics,\n"
        "    trace_scope,\n"
        ")\n"
        "from app.tool.context import ToolContext, tool_context_scope\n",
    )

old_sync = '''        with tool_context_scope(ctx):
            return meta.impl(**kwargs)
'''
new_sync = '''        # 线程池不继承 contextvars：trace_id 和 ToolContext 都要在这里重新挂上
        with trace_scope(ctx.trace_id), tool_context_scope(ctx):
            return meta.impl(**kwargs)
'''
assert old_sync in s, "guard sync anchor not found"
s = s.replace(old_sync, new_sync)

old_async = '''        with tool_context_scope(ctx):
            return await meta.impl(**kwargs)
'''
new_async = '''        # 协程被桥接到主事件循环后同样丢失 contextvars，一并恢复
        with trace_scope(ctx.trace_id), tool_context_scope(ctx):
            return await meta.impl(**kwargs)
'''
assert old_async in s, "guard async anchor not found"
s = s.replace(old_async, new_async)

# run() 末尾埋点
old_tail = '''        result = self._invoke_with_retry(meta, kwargs, ctx)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._audit(meta, ctx, kwargs, result)
        return result
'''
new_tail = '''        result = self._invoke_with_retry(meta, kwargs, ctx)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)

        status = "ok" if result.ok else (result.blocked or "error")
        metrics.inc(M_TOOL_CALLS, 1, tool=meta.name, status=status)
        metrics.observe(M_TOOL_DURATION, result.elapsed_ms, tool=meta.name)

        self._audit(meta, ctx, kwargs, result)
        return result
'''
assert old_tail in s, "guard run tail anchor not found"
s = s.replace(old_tail, new_tail)

io.open(p, "w", encoding="utf-8").write(s)
print("[ok] guard.py trace scope + metrics")

# ---------------------------------------------------------------- tool_executor.py
p = "app/agent/tool_executor.py"
s = io.open(p, encoding="utf-8").read()

if "get_trace_id" not in s:
    s = s.replace(
        "from app.tool.context import ToolContext, tool_context_scope\n",
        "from app.core.obs import get_trace_id\n"
        "from app.tool.context import ToolContext, tool_context_scope\n",
    )
    old = '''        ctx = ToolContext(
            user_id=user_id,
            conversation_id=conversation_id,
            loop=loop,
        )
'''
    new = '''        ctx = ToolContext(
            user_id=user_id,
            conversation_id=conversation_id,
            loop=loop,
            # 显式带上：工具在线程池/主循环里执行时才能把这个 trace 续上
            trace_id=get_trace_id(),
        )
'''
    assert old in s, "executor anchor not found"
    s = s.replace(old, new)
    io.open(p, "w", encoding="utf-8").write(s)
    print("[ok] tool_executor.py passes trace_id")
else:
    print("[skip] tool_executor.py already wired")
