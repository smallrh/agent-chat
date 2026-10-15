"""诊断：复现「工具执行完就没有第二轮 LLM、没有最终回复」的问题。

独立进程跑一次 Agent.stream，逐事件打印耗时；
另起一个 watchdog，超时后强制打印当前卡点并退出（避免脚本自己挂死）。
"""

import asyncio
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
# httpx / openai 的请求日志单独开到 INFO
logging.getLogger("httpx").setLevel(logging.INFO)

from app.agent.agent import agent          # noqa: E402
from app.core.config import settings       # noqa: E402

T0 = time.perf_counter()


def ts() -> str:
    return f"+{time.perf_counter() - T0:7.2f}s"


async def watchdog(limit: float = 75.0):
    """超时后硬退出，避免诊断脚本自己卡死。"""
    await asyncio.sleep(limit)
    print(f"\n[{ts()}] !!!!! WATCHDOG TIMEOUT {limit}s —— 诊断脚本强制退出 !!!!!")
    print("（若走到这里，说明 Agent.stream 在某个 await 上永久挂住）")
    os._exit(2)


async def main():
    print(f"[{ts()}] MAX_TOOL_ROUNDS={settings.MAX_TOOL_ROUNDS} "
          f"TOOL_PARALLEL_ENABLED={settings.TOOL_PARALLEL_ENABLED} "
          f"TOOL_MAX_PARALLEL={settings.TOOL_MAX_PARALLEL} "
          f"LLM_TIMEOUT={settings.LLM_TIMEOUT_SECONDS}")
    print(f"[{ts()}] model={settings.OPENAI_MODEL}")

    asyncio.create_task(watchdog(75.0))

    msg = ("现在告诉我广州的天气，然后还有计算22的5000次方是多少，"
           "另外介绍一下广州琶洲岛")

    n = 0
    async for event in agent.stream(
        message=msg,
        history=None,
        summary=None,
        memories=None,
        profile=None,
        cancel_token=None,
        user_id=1,
        conversation_id="diag-stream-hang",
    ):
        n += 1
        et = event.get("type")
        extra = ""
        if et == "message.delta":
            extra = repr(event.get("content"))[:120]
        elif et in ("tool.start", "tool.result", "tool.end"):
            extra = (
                f"{event.get('tool_name')} "
                f"result={str(event.get('result'))[:80]!r}"
            )
        print(f"[{ts()}] EVENT[{n}] {et} {extra}")

    print(f"[{ts()}] ===== stream finished, events={n} =====")


if __name__ == "__main__":
    asyncio.run(main())
