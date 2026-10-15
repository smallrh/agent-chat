"""抓一次 Exa Responses API 的原始 SSE 流，看清楚是什么内容让 SDK 解析失败。

只打前若干条事件的类型与 data 预览，不落完整正文。
消耗 1 次搜索配额。
"""

import json
import sys

import httpx

from app.core.config import settings

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "综合回答。"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                "url": {"type": "string"},
                    "published_date": {"type": "string"},
                },
                "required": ["title", "url"],
                "additionalProperties": False,
            },
            "description": "引用的来源列表。",
        },
    },
    "required": ["answer", "sources"],
    "additionalProperties": False,
}


def main(argv):
    key = settings.EXA_API_KEY or settings.WEB_SEARCH_API_KEY
    if not key:
        print("no exa key configured")
        return 1

    query = argv[0] if argv else "广州琶洲岛 介绍"
    payload = {
        "model": "agent",
        "input": query,
        "stream": True,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "agent_output",
                "schema": SCHEMA,
            }
        },
    }

    print("=" * 66)
    print("Exa SSE 原始流抓取")
    print("=" * 66)
    print(f"query: {query!r}\n")

    lines_seen = 0
    types: dict[str, int] = {}
    empty_data = 0
    started = time.perf_counter()

    try:
        with httpx.stream(
            "POST",
            "https://api.exa.ai/responses",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        ) as resp:
            print(f"HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if lines_seen >= 40:
                    break
                if not line:
                    continue
                lines_seen += 1
                if line.startswith("event:"):
                    t = line[6:].strip()
                    types[t] = types.get(t, 0) + 1
                    print(f"  event: {t}")
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    if not raw:
                        empty_data += 1
                        print("  data: <EMPTY>          <<< SDK 会在这里炸")
                    else:
                        print(f"  data: {raw[:100]}")
                else:
                    print(f"  other: {line[:60]}")
    except Exception as exc:
        print(f"request failed: {type(exc).__name__}: {exc}")
        return 1

    print(f"\n耗时 {time.perf_counter():.2f}s")
    print(f"事件类型分布: {types}")
    print(f"空 data 行数: {empty_data}")
    return 0


if __name__ == "__main__":
    import time
    sys.exit(main(sys.argv[1:]))
