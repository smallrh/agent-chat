from pathlib import Path

ws = Path(r"D:\work\agent_system\agent\app\tool\web_search.py")
t = ws.read_text(encoding="utf-8")
start = t.find("def _exa_search(query: str, top_k: int = 5) -> str:")
end = t.find("\ndef _consume_sse")
if start < 0 or end < 0:
    raise SystemExit(f"markers not found start={start} end={end}")

new_fn = '''def _exa_search(query: str, top_k: int = 5) -> str:
    """调 Exa Responses API。优先非流式 JSON，避免 SSE 永不 completed 卡死。"""
    api_key = settings.EXA_API_KEY or settings.WEB_SEARCH_API_KEY
    if not api_key:
        return (
            "未配置 Exa API Key（请在 .env 设置 EXA_API_KEY，"
            "或将 WEB_SEARCH_PROVIDER 设为 tavily）。"
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    schema = {
        "format": {
            "type": "json_schema",
            "name": "agent_output",
            "schema": _EXA_OUTPUT_SCHEMA,
        }
    }

    try:
        resp = httpx.post(
            _EXA_ENDPOINT,
            headers=headers,
            json={"model": "agent", "input": query, "stream": False, "text": schema},
            timeout=_EXA_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        text = (data.get("output_text") or "") if isinstance(data, dict) else ""
        if not text and isinstance(data, dict):
            for item in data.get("output") or []:
                if not isinstance(item, dict):
                    continue
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("text"):
                        text = c["text"]
                        break
                if text:
                    break
        if text.strip():
            return _format_exa_result(text.strip(), top_k, query)
    except Exception:
        logger.exception("Exa non-stream search failed, fallback to SSE")

    payload = {
        "model": "agent",
        "input": query,
        "stream": True,
        "text": schema,
    }
    try:
        with httpx.stream(
            "POST",
            _EXA_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=_EXA_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            text = _consume_sse(resp)
    except Exception as e:
        logger.exception("Exa search failed")
        return f"[web_search] Exa 检索失败：{e}"

    if not text.strip():
        return f"[web_search] Exa 未检索到相关结果（query={query!r}）。"

    return _format_exa_result(text.strip(), top_k, query)


'''
ws.write_text(t[:start] + new_fn + t[end:], encoding="utf-8")
print("web_search ok")

ag = Path(r"D:\work\agent_system\agent\app\agent\agent.py")
t = ag.read_text(encoding="utf-8")
start = t.find("            if parallel:\n                raw = await asyncio.gather(")
end = t.find("        # =========================\n        # Agent 内部结束")
if start < 0 or end < 0:
    raise SystemExit(f"agent markers start={start} end={end}")

new_block = '''            results: list[str | None] = [None] * len(items)

            async def _run_one(idx: int, it: dict) -> None:
                if cancel_token is not None and cancel_token.cancelled:
                    results[idx] = "已取消"
                    return
                try:
                    results[idx] = await tool_executor.aexecute(
                        it["name"],
                        it["arguments"],
                        user_id=user_id,
                        conversation_id=conversation_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    results[idx] = f"工具执行失败：{exc}"

            if parallel:
                pending = {
                    asyncio.create_task(_run_one(i, it)): i
                    for i, it in enumerate(items)
                }
                remaining = set(pending)
                while remaining:
                    done, remaining = await asyncio.wait(
                        remaining, return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        idx = pending[task]
                        item = items[idx]
                        tool_result = results[idx] or ""
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
            else:
                for idx, it in enumerate(items):
                    if cancel_token is not None and cancel_token.cancelled:
                        raise AgentCancelled()
                    await _run_one(idx, it)
                    tool_result = results[idx] or ""
                    yield {
                        "type": "tool.result",
                        "tool_call_id": it["id"],
                        "tool_name": it["name"],
                        "result": tool_result,
                    }
                    yield {
                        "type": "tool.end",
                        "tool_call_id": it["id"],
                        "tool_name": it["name"],
                    }

            for item, tool_result in zip(items, results):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["id"],
                        "content": tool_result or "",
                    }
                )

'''
ag.write_text(t[:start] + new_block + t[end:], encoding="utf-8")
print("agent ok")
