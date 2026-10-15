from pathlib import Path

p = Path(__file__).with_name("app") / "agent" / "agent.py"
t = p.read_text(encoding="utf-8")

old = "import re\n\nfrom app.agent.llm import llm"
new = "import asyncio\nimport re\n\nfrom app.agent.llm import llm"
if old not in t:
    raise SystemExit("import block not found")
t = t.replace(old, new, 1)

insert = """
_STREAM_END = object()


def _sync_next(it):
    try:
        return next(it)
    except StopIteration:
        return _STREAM_END


"""
needle = "from app.core.config import settings\n\n\nclass Agent:"
if needle not in t:
    raise SystemExit("class needle not found")
t = t.replace(needle, "from app.core.config import settings\n" + insert + "class Agent:", 1)

old_first = """        response = llm.chat(
            messages=messages,
            stream=True,
        )

        content_parts = []

        tool_calls = {}

        # =========================
        # 2. 流式读取
        # =========================

        for chunk in response:
"""
new_first = """        response = await asyncio.to_thread(
            llm.chat,
            messages,
            True,
        )

        content_parts = []

        tool_calls = {}

        # =========================
        # 2. 流式读取（线程里 next，避免阻塞事件循环）
        # =========================

        stream_iter = iter(response)
        while True:
            chunk = await asyncio.to_thread(_sync_next, stream_iter)
            if chunk is _STREAM_END:
                break
"""
if old_first not in t:
    raise SystemExit("first stream loop not found")
t = t.replace(old_first, new_first, 1)

old_tool = """            tool_result = tool_executor.execute(
                tool_name=tool_name,
                arguments=item["arguments"],
            )
"""
new_tool = """            tool_result = await asyncio.to_thread(
                tool_executor.execute,
                tool_name,
                item["arguments"],
            )
"""
if old_tool not in t:
    raise SystemExit("tool execute not found")
t = t.replace(old_tool, new_tool, 1)

old_final = """        final_response = llm.chat(
            messages=messages,
            stream=True,
        )

        for chunk in final_response:
"""
new_final = """        final_response = await asyncio.to_thread(
            llm.chat,
            messages,
            True,
        )

        final_iter = iter(final_response)
        while True:
            chunk = await asyncio.to_thread(_sync_next, final_iter)
            if chunk is _STREAM_END:
                break
"""
if old_final not in t:
    raise SystemExit("final stream not found")
t = t.replace(old_final, new_final, 1)

p.write_text(t, encoding="utf-8")
print("patched", p)
