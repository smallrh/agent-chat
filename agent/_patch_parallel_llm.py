"""改动：
1. agent.py：本轮多个 tool_calls 改为并发执行（顺序保持）
2. llm.py：埋点（调用次数 / 耗时 / token 用量）
3. config.py：新增 TOOL_PARALLEL_ENABLED / TOOL_MAX_PARALLEL
"""

import io

# ---------------------------------------------------------------- config.py
p = "app/core/config.py"
s = io.open(p, encoding="utf-8").read()
anchor = "    # 同步工具执行线程池大小\n    TOOL_EXECUTOR_WORKERS :int = 8\n"
assert anchor in s, "config anchor missing"
if "TOOL_PARALLEL_ENABLED" not in s:
    s = s.replace(anchor, anchor + """
    # 本轮有多个独立工具调用时并发执行（无依赖的工具，如同时查天气和当前时间）。
    # 注意：并发不改变 messages 里的顺序，结果仍按 tool_calls 原始顺序回填。
    TOOL_PARALLEL_ENABLED :bool = True
    # 单轮最多并发多少个工具（超出则退回串行，避免一次把线程池打满）
    TOOL_MAX_PARALLEL :int = 4
""")
    io.open(p, "w", encoding="utf-8").write(s)
    print("[ok] config.py + TOOL_PARALLEL_*")
else:
    print("[skip] config.py already has TOOL_PARALLEL_*")

# ---------------------------------------------------------------- agent.py
p = "app/agent/agent.py"
s = io.open(p, encoding="utf-8").read()

old = '''            # =========================
            # 逐个执行工具，结果塞回 messages，然后进入下一轮
            # =========================

            for item in tool_calls.values():
                tool_call_id = item["id"]
                tool_name = item["name"]

                if cancel_token is not None and cancel_token.cancelled:
                    raise AgentCancelled()

                yield {
                    "type": "tool.start",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": item["arguments"],
                }

                tool_result = await asyncio.to_thread(
                    tool_executor.execute,
                    tool_name,
                    item["arguments"],
                    user_id=user_id,
                    conversation_id=conversation_id,
                    loop=asyncio.get_running_loop(),
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
'''

new = '''            # =========================
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

            loop = asyncio.get_running_loop()

            def _invoke(it: dict) -> str:
                return tool_executor.execute(
                    it["name"],
                    it["arguments"],
                    user_id=user_id,
                    conversation_id=conversation_id,
                    loop=loop,
                )

            parallel = (
                settings.TOOL_PARALLEL_ENABLED
                and len(items) > 1
                and len(items) <= settings.TOOL_MAX_PARALLEL
            )

            if parallel:
                raw = await asyncio.gather(
                    *[asyncio.to_thread(_invoke, it) for it in items],
                    return_exceptions=True,
                )
                results: list[str] = [
                    r if isinstance(r, str) else f"工具执行失败：{r}"
                    for r in raw
                ]
            else:
                results = []
                for it in items:
                    # 串行路径保留每步的取消检查（并发路径无法中途取消）
                    if cancel_token is not None and cancel_token.cancelled:
                        raise AgentCancelled()
                    try:
                        results.append(await asyncio.to_thread(_invoke, it))
                    except Exception as exc:                  # noqa: BLE001
                        # tool_executor 已把异常转成文案，这里是双保险
                        results.append(f"工具执行失败：{exc}")

            for item, tool_result in zip(items, results):
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

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["id"],
                        "content": tool_result,
                    }
                )
'''

assert old in s, "agent.py tool loop anchor not found"
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8").write(s)
print("[ok] agent.py parallel tool execution")

# ---------------------------------------------------------------- llm.py
p = "app/agent/llm.py"
s = io.open(p, encoding="utf-8").read()

if "M_LLM_CALLS" not in s:
    s = s.replace(
        "from app.core.config import settings\nfrom app.tool.registry import TOOL_DEFINITIONS\n",
        "import time\n\n"
        "from app.core.config import settings\n"
        "from app.core.obs import (\n"
        "    M_LLM_CALLS,\n"
        "    M_LLM_DURATION,\n"
        "    M_LLM_TOKENS,\n"
        "    metrics,\n"
        ")\n"
        "from app.tool.registry import TOOL_DEFINITIONS\n",
    )

    old_ret = '''        return self.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
            max_tokens=settings.MAX_TOKENS,
            temperature=settings.TEMPERATURE,
            stream=stream,
            timeout=req_timeout,
        )
'''
    new_ret = '''        started = time.perf_counter()
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
'''
    assert old_ret in s, "llm.py return anchor not found"
    s = s.replace(old_ret, new_ret)
    io.open(p, "w", encoding="utf-8").write(s)
    print("[ok] llm.py metrics")
else:
    print("[skip] llm.py already instrumented")
