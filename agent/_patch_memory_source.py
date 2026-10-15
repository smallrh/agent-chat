"""新增 GET /memories/{memory_id}/source —— 记忆溯源接口。"""

import io

p = "app/api/memory.py"
s = io.open(p, encoding="utf-8").read()

if "memory_source" in s:
    print("[skip] memory.py already has source endpoint")
    raise SystemExit(0)

# 0) 补 logging（原文件没有 logger）
if "logger = logging.getLogger" not in s:
    s = s.replace(
        "from fastapi import APIRouter, Depends, HTTPException, Query",
        "import logging\n\nfrom fastapi import APIRouter, Depends, HTTPException, Query",
    )
    s = s.replace(
        'router = APIRouter(prefix="/memories", tags=["Memory"])',
        'logger = logging.getLogger(__name__)\n\n'
        'router = APIRouter(prefix="/memories", tags=["Memory"])',
    )
    print("[ok] memory.py + logger")

# 1) 响应模型
anchor_model = '''class MemoryUpdateResponse(BaseModel):
    new_memory_id: str | None = None
    message: str
'''
new_model = '''class MemoryUpdateResponse(BaseModel):
    new_memory_id: str | None = None
    message: str


class SourceMessage(BaseModel):
    message_id: str
    role: str
    content: str
    sequence_id: int | None = None
    created_at: str | None = None


class MemorySourceResponse(BaseModel):
    """记忆溯源：这条记忆从哪来。"""
    memory_id: str
    content: str
    memory_type: str | None = None
    slot: str | None = None
    source_conversation_id: str | None = None
    extract_model: str | None = None
    prompt_version: str | None = None
    embedding_model: str | None = None
    created_at: str | None = None
    messages: list[SourceMessage] = []
'''
assert anchor_model in s, "model anchor missing"
s = s.replace(anchor_model, new_model)

# 2) 端点 + helper
anchor_ep = '''# =====================================================================
# POST /memories/retrieve  — 语义检索（产品核心价值 / 读取端）
# =====================================================================
'''
new_ep = '''# =====================================================================
# GET /memories/{memory_id}/source  — 记忆溯源
# =====================================================================


async def _fetch_source_messages(
    message_ids: list[str],
    user_id: int,
) -> list[dict]:
    """按 message_id 取原始消息内容。

    直接用共享池查询，不经过 MessageRepository —— 只读溯源查询，
    没必要为此让仓储层多暴露一个方法。user_id 过滤保证不串用户。
    """
    if not message_ids:
        return []

    from app.repo.pg import get_pool

    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT message_id, role, content, sequence_id, created_at
        FROM messages
        WHERE message_id = ANY($1::uuid[])
          AND user_id = $2
        ORDER BY sequence_id
        """,
        [str(m) for m in message_ids],
        int(user_id),
    )
    return [
        {
            "message_id": str(row["message_id"]),
            "role": row["role"],
            "content": row["content"],
            "sequence_id": row["sequence_id"],
            "created_at": row["created_at"].isoformat()
            if row["created_at"] else None,
        }
        for row in rows
    ]


@router.get("/{memory_id}/source", response_model=MemorySourceResponse)
async def memory_source(
    memory_id: str,
    user_id: int = Depends(get_current_user_id),
):
    """这条记忆是从哪句话抽出来的。

    用于排查「为什么会有这条记忆 / 它是不是理解错了」：
    返回原始消息 + 当时的抽取模型与 prompt 版本。
    """
    item = await memory_crud_service.get_memory(user_id, memory_id)
    if not item:
        raise HTTPException(status_code=404, detail="memory not found or not owned")

    raw_ids = item.get("source_message_ids") or []
    if isinstance(raw_ids, str):
        try:
            import json

            raw_ids = json.loads(raw_ids)
        except ValueError:
            raw_ids = []

    try:
        messages = await _fetch_source_messages(list(raw_ids), user_id)
    except Exception:                                     # noqa: BLE001
        logger.warning(
            "memory source fetch failed: memory_id=%s user_id=%s",
            memory_id, user_id, exc_info=True,
        )
        messages = []

    return MemorySourceResponse(
        memory_id=str(item.get("memory_id") or memory_id),
        content=item.get("content") or "",
        memory_type=item.get("memory_type"),
        slot=item.get("slot"),
        source_conversation_id=item.get("source_conversation_id"),
        extract_model=item.get("extract_model"),
        prompt_version=item.get("prompt_version"),
        embedding_model=item.get("embedding_model"),
        created_at=item.get("created_at"),
        messages=messages,
    )


# =====================================================================
# POST /memories/retrieve  — 语义检索（产品核心价值 / 读取端）
# =====================================================================
'''
assert anchor_ep in s, "endpoint anchor missing"
s = s.replace(anchor_ep, new_ep)

io.open(p, "w", encoding="utf-8").write(s)
print("[ok] memory.py + /memories/{id}/source")
