import io

p = "app/tool/web_search.py"
s = io.open(p, encoding="utf-8").read()
assert "def _tavily_search(" in s, "_tavily_search missing"

if "TOOL_SPEC" in s and "TOOL_CONFIG" in s:
    print("[skip] registration metadata already present")
    raise SystemExit(0)

tail = '''

# ---------------------------------------------------------------------------
# 工具注册元数据（registry 自动发现，见 app/tool/spec.py）
# ---------------------------------------------------------------------------

TOOL_SPEC = {
    "description": (
        "实时联网搜索，返回与问题相关的网页标题、链接与摘要。"
        "用于需要最新资讯或训练数据之外知识的场景。"
    ),
    "parameters": {
        "query": {
            "type": "string",
            "description": "搜索关键词",
        },
        "top_k": {
            "type": "integer",
            "description": "返回条数，默认5",
        },
    },
    "required": ["query"],
}

TOOL_CONFIG = {
    # 实测 Exa agent 模式单次调用 15~37s，原来的 30s 超时必然触发；
    # 超时后 retries=1 会再吃一份时间且最终仍失败 —— 得不偿失。
    # 现在：超时放宽到 60s 让请求能跑完，retries=0 失败即快速降级。
    "timeout": 60,
    "retries": 0,
    "quota_per_minute": 20,
}
'''

s = s.rstrip("\n") + "\n" + tail
io.open(p, "w", encoding="utf-8").write(s)
print("[ok] web_search.py + TOOL_SPEC/TOOL_CONFIG")
