"""并行工具链路耗时诊断（定位「很慢」到底慢在哪）。

测量：
  1. calculator 大整数：确认不再抛 ValueError，且返回可控长度
  2. 一轮多工具「并发 vs 串行」：用 200ms 慢工具（毫秒级工具的
     线程调度开销会盖过并发收益，测不出东西）
  3. 可选：联网工具耗时（Exa），会消耗搜索配额，需显式 --with-network

运行：
    .venv/Scripts/python.exe _diag_parallel.py [--with-network]
"""

import asyncio
import json
import sys
import time

FAILED = 0


def check(desc, cond, extra=""):
    global FAILED
    if cond:
        print(f"  [ok] {desc}")
    else:
        FAILED += 1
        print(f"  [FAIL] {desc} {extra}")


async def time_call(executor, name, args, ctx, loop):
    started = time.perf_counter()
    try:
        out = await executor.aexecute(
            name, json.dumps(args),
            user_id=ctx.user_id, conversation_id=ctx.conversation_id,
            loop=loop,
        )
    except Exception as exc:                              # noqa: BLE001
        out = f"EXC: {exc}"
    return time.perf_counter() - started, out


async def main(argv):
    from app.agent.tool_executor import tool_executor
    from app.tool.context import ToolContext
    from app.tool.guard import tool_guard
    from app.tool.spec import ToolConfig, ToolMeta

    with_network = "--with-network" in argv

    ctx = ToolContext(user_id=1, conversation_id="diag")
    loop = asyncio.get_running_loop()

    print("=" * 66)
    print("并行工具链路耗时诊断")
    print("=" * 66)

    # ------------------------------------------------------------------
    print("\n[1] calculator 大整数（原来会抛 ValueError 触发模型重试风暴）")
    cases = ["2**10", "22**1000", "22**5000", "22**10000"]
    for expr in cases:
        elapsed, out = await time_call(
            tool_executor, "calculator", {"expression": expr}, ctx, loop,
        )
        preview = out if len(out) <= 48 else out[:48] + "..."
        print(f"  {expr:12s} {elapsed * 1000:7.1f} ms  len={len(out):5d}  {preview}")

    # ------------------------------------------------------------------
    print("\n[2] 一轮多工具：并发 vs 串行（每个工具 200ms）")

    def slow(ms: int):
        time.sleep(ms / 1000)
        return "ok"

    meta = ToolMeta(
        name="diag_slow", impl=slow,
        definition={"type": "function", "function": {"name": "diag_slow"}},
        config=ToolConfig(timeout=15, retries=0),
    )

    tool_guard.reset()

    start = time.perf_counter()
    for _ in range(3):
        await tool_guard.arun(meta, {"ms": 200}, ctx)
    serial_time = time.perf_counter() - start

    start = time.perf_counter()
    await asyncio.gather(
        *[tool_guard.arun(meta, {"ms": 200}, ctx) for _ in range(3)]
    )
    parallel_time = time.perf_counter() - start

    speedup = serial_time / parallel_time if parallel_time else 0
    print(f"  串行 3×200ms: {serial_time * 1000:8.1f} ms")
    print(f"  并发 3×200ms: {parallel_time * 1000:8.1f} ms")
    print(f"  加速比:        {speedup:8.2f}x")

    check("并发确实生效（加速比 > 2x）", speedup > 2.0, f"实际={speedup:.2f}x")

    # ------------------------------------------------------------------
    if with_network:
        print("\n[3] 联网工具耗时（消耗搜索配额）")
        elapsed, out = await time_call(
            tool_executor, "web_search",
            {"query": "广州琶洲岛 介绍", "top_k": 5}, ctx, loop,
        )
        print(f"  web_search: {elapsed * 1000:8.1f} ms  len={len(out)}")
    else:
        print("\n[3] 联网工具耗时 —— 跳过（加 --with-network 启用）")

    print()
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
