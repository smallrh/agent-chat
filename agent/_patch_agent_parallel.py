import io

p = "app/agent/agent.py"
s = io.open(p, encoding="utf-8").read()

old = '''            loop = asyncio.get_running_loop()

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
'''

new = '''            parallel = (
                settings.TOOL_PARALLEL_ENABLED
                and len(items) > 1
                and len(items) <= settings.TOOL_MAX_PARALLEL
            )

            if parallel:
                raw = await asyncio.gather(
                    *[
                        tool_executor.aexecute(
                            it["name"],
                            it["arguments"],
                            user_id=user_id,
                            conversation_id=conversation_id,
                        )
                        for it in items
                    ],
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
                        results.append(
                            await tool_executor.aexecute(
                                it["name"],
                                it["arguments"],
                                user_id=user_id,
                                conversation_id=conversation_id,
                            )
                        )
                    except Exception as exc:                  # noqa: BLE001
                        # tool_executor 已把异常转成文案，这里是双保险
                        results.append(f"工具执行失败：{exc}")
'''

assert old in s, "agent parallel anchor missing"
s = s.replace(old, new, 1)
io.open(p, "w", encoding="utf-8").write(s)
print("[ok] agent.py 并行分支改用 aexecute（不再双层线程池）")
