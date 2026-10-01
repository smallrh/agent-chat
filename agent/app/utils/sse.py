import json
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    """UTC ISO 8601，精度到微秒，尾部 Z。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def sse(event: str, data: dict[str, Any]) -> str:
    """
    把事件编码成标准 SSE 格式。

    自动在 data 里补 timestamp（如果调用方没传）。

    用法：
        yield sse("message.delta", {"message_id": "...", "content": "你好"})

    输出：
        event: message.delta
        data: {"message_id":"...","content":"你好","timestamp":"2026-09-23T18:20:00.650Z"}

    """
    if "timestamp" not in data:
        data = {"timestamp": _now_iso(), **data}

    body = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return f"event: {event}\ndata: {body}\n\n"
