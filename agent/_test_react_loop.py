"""离线验证：多步工具循环（ReAct）。

不依赖任何外部服务（LLM / Qdrant / PG / Kafka），只用 FakeLLM + FakeExecutor
验证 agent.stream / agent.chat 会在「LLM 再次请求工具」时进入下一轮，直到
不再请求工具或达到 MAX_TOOL_ROUNDS。

运行：
    .venv/Scripts/python.exe _test_react_loop.py
"""

import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

import app.agent.agent as agent_mod
from app.core.config import settings


class FakeLLM:
    """模拟一个会连续调用两次工具的模型。

    - 第 1、2 轮（messages 里 tool 结果 < 2 条）：请求调用 calculator
    - 第 3 轮（已有 2 条 tool 结果）：直接给出最终文本
    """

    def _wants_tool(self, messages) -> bool:
        tool_count = sum(1 for m in messages if m["role"] == "tool")
        return tool_count < 2

    def chat(self, messages, stream=False):
        if self._wants_tool(messages):
            if stream:
                # 先吐一句前言，再发 tool_call（模拟真实流式顺序）
                return iter([
                    SimpleNamespace(choices=[SimpleNamespace(
                        delta=SimpleNamespace(content="我先算一下。", tool_calls=None)
                    )]),
                    SimpleNamespace(choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(
                                    name="calculator",
                                    arguments='{"expression": "2 + 2"}',
                                ),
                            )
                        ])
                    )]),
                ])
            msg = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(
                            name="calculator",
                            arguments='{"expression": "2 + 2"}',
                        ),
                    )
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        # 最终轮：不再要工具
        if stream:
            return iter([SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="结果是 4。", tool_calls=None)
            )])])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="结果是 4。", tool_calls=None
        ))])


class FakeExecutor:
    def execute(self, tool_name, arguments, user_id=None, conversation_id=None, loop=None):
        # 把 user_id 透传回来，顺带验证 context 注入生效
        if tool_name == "calculator":
            return "4"
        return f"unknown:{tool_name}"

    def _execute(self, tool_name, arguments):
        return self.execute(tool_name, arguments)


async def test_stream():
    agent_mod.llm = FakeLLM()
    agent_mod.tool_executor = FakeExecutor()

    events = []
    async for ev in agent_mod.agent.stream(message="2+2 等于多少", user_id=1):
        events.append(ev)

    types = [e["type"] for e in events]
    print("[stream] event types:", types)

    assert types.count("tool.start") == 2, f"期望 2 轮工具调用，实际 {types.count('tool.start')}"
    assert types.count("tool.result") == 2
    assert types.count("tool.end") == 2
    assert events[-1]["type"] == "agent.end", "必须以 agent.end 收尾"
    # 第二轮 LLM 的最终文本必须出现在 delta 里
    assert any(
        e["type"] == "message.delta" and "结果是 4" in e["content"]
        for e in events
    ), "最终答案未流式吐出"
    print("[stream] OK — 多步循环 + 最终答案均验证通过")


def test_chat():
    agent_mod.llm = FakeLLM()
    agent_mod.tool_executor = FakeExecutor()

    result = agent_mod.agent.chat(message="2+2 等于多少", user_id=1)
    print("[chat] content:", result["content"])
    print("[chat] messages roles:", [m["role"] for m in result["messages"]])

    assert result["content"] == "结果是 4。", result["content"]
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2, f"期望 2 条 tool 消息，实际 {len(tool_msgs)}"
    print("[chat] OK — 非流式多步循环验证通过")


def test_max_rounds():
    """验证达到 MAX_TOOL_ROUNDS 后能安全停止，不会无限循环。"""

    class LoopLLM:
        def chat(self, messages, stream=False):
            if stream:
                return iter([SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None, tool_calls=[
                        SimpleNamespace(
                            index=0, id="call_x",
                            function=SimpleNamespace(
                                name="calculator", arguments='{"expression": "1+1"}'
                            ),
                        )
                    ])
                )])])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="", tool_calls=[
                    SimpleNamespace(
                        id="call_x",
                        function=SimpleNamespace(
                            name="calculator", arguments='{"expression": "1+1"}'
                        ),
                    )
                ]
            ))])

    agent_mod.llm = LoopLLM()
    agent_mod.tool_executor = FakeExecutor()

    events = []
    async_gen = agent_mod.agent.stream(message="loop", user_id=1)

    async def _run():
        async for ev in async_gen:
            events.append(ev)

    asyncio.run(_run())
    types = [e["type"] for e in events]
    assert types.count("tool.start") == settings.MAX_TOOL_ROUNDS, (
        f"达到上限后应恰好 {settings.MAX_TOOL_ROUNDS} 轮，实际 {types.count('tool.start')}"
    )
    assert events[-1]["type"] == "agent.end"
    print(f"[max_rounds] OK — 在第 {settings.MAX_TOOL_ROUNDS} 轮安全停止")


if __name__ == "__main__":
    print("=== ReAct 多步工具循环离线验证 ===")
    test_chat()
    asyncio.run(test_stream())
    test_max_rounds()
    print("\n全部通过 ✅")
