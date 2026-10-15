"""实测 Exa 官方示例的流式写法在当前环境是否可用。

用户贴的官方示例核心参数：
    model="agent", input=<query>, stream=True,
    text={"format": {"type": "json_schema", "name": "agent_output", "schema": {...}}}

这里对比两种写法：
  A) 官方原样：schema = {"type": "object"}（空对象）
  B) 项目实际：schema = 完整 answer + sources 结构

每次调用消耗 1 次搜索配额。
"""

import sys
import time

from openai import OpenAI

from app.core.config import settings

EMPTY_SCHEMA = {"type": "object"}

FULL_SCHEMA = {
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


def run(client, schema, query):
    started = time.perf_counter()
    text = ""
    try:
        stream = client.responses.create(
            model="agent",
            input=query,
            stream=True,
            text={"format": {"type": "json_schema",
                             "name": "agent_output",
                             "schema": schema}},
        )
        for event in stream:
            if event.type == "response.completed":
                text = event.response.output_text
        return time.perf_counter() - started, text, None
    except Exception as exc:                              # noqa: BLE001
        return time.perf_counter() - started, "", f"{type(exc).__name__}: {exc}"


def main(argv):
    key = settings.EXA_API_KEY or settings.WEB_SEARCH_API_KEY
    if not key:
        print("no exa key configured")
        return 1

    client = OpenAI(base_url="https://api.exa.ai", api_key=key, timeout=120)
    query = argv[0] if argv else "广州琶洲岛 介绍"

    print("=" * 66)
    print("Exa 流式写法实测")
    print("=" * 66)
    print(f"query: {query!r}\n")

    for label, schema in (("官方空 schema", EMPTY_SCHEMA), ("项目完整 schema", FULL_SCHEMA)):
        elapsed, text, err = run(client, schema, query)
        if err:
            print(f"  [{label}] {elapsed:6.2f}s  ERROR -> {err[:120]}")
        else:
            print(f"  [{label}] {elapsed:6.2f}s  len={len(text or '')}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
