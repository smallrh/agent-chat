"""Memory CRUD API 路由。

4 个端点：
  GET    /memories                 → 列表（分页 + 按 type 过滤）
  GET    /memories/{memory_id}     → 单条详情（带归属校验）
  DELETE /memories/{memory_id}     → 软删（PG + Qdrant + Redis 级联）
  PATCH  /memories/{memory_id}     → 版本化修改（新 INSERT + 老行 is_active=false）
"""
from __future__ import annotations

from typing import Literal

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import get_current_user_id
from app.service.memory_service import memory_crud_service
from app.service.retrieval_service import memory_retrieval_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memories", tags=["Memory"])


# =====================================================================
# Pydantic 请求/响应模型
# =====================================================================

class MemoryItem(BaseModel):
    memory_id: str
    conversation_id: str | None = None
    memory_type: str
    slot: str | None = None
    content: str
    importance: float
    confidence: float | None = None
    metadata: dict | None = None
    sync_status: str
    sync_retry_count: int
    sync_error: str | None = None
    synced_at: str | None = None
    source_message_ids: list[str] | None = None
    extract_model: str | None = None
    prompt_version: str | None = None
    embedding_model: str | None = None
    is_active: bool
    merged_into: str | None = None
    deleted_at: str | None = None
    created_at: str
    updated_at: str


class MemoryListResponse(BaseModel):
    items: list[MemoryItem]
    total: int | None = None  # 可选，没做 count 优化就返回 None


class MemoryDeleteResponse(BaseModel):
    pg: bool
    qdrant: bool
    redis: bool


class MemoryUpdateRequest(BaseModel):
    new_content: str | None = Field(None, description="新版本内容")
    new_importance: float | None = Field(None, ge=0.0, le=1.0, description="重要性 0~1")


class MemoryUpdateResponse(BaseModel):
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


# =====================================================================
# 检索（读取端）请求/响应模型
# =====================================================================

class MemoryRetrieveRequest(BaseModel):
    query: str = Field(..., description="自然语言检索语句", min_length=1, max_length=2000)
    memory_types: list[str] | None = Field(
        None, description="限定记忆类型，如 ['preference','name']；None=全部类型"
    )
    top_k: int = Field(10, ge=1, le=50, description="最多返回几条")
    score_threshold: float | None = Field(
        None, ge=0.0, le=1.0, description="cosine 下限；None=用配置默认值(0.35)"
    )
    record_access: bool = Field(
        False, description="是否顺带 +1 访问计数（独立 write，不影响主链路回表）"
    )


class MemoryRetrieveItem(BaseModel):
    memory_id: str
    memory_type: str
    content: str
    score: float
    importance: float | None = None
    confidence: float | None = None
    created_at: str | None = None
    metadata: dict | None = None


class MemoryRetrieveResponse(BaseModel):
    query: str
    count: int
    results: list[MemoryRetrieveItem]


# =====================================================================
# GET /memories
# =====================================================================

@router.get("", response_model=MemoryListResponse)
async def list_memories(
    active_only: bool = Query(True, description="只返回 is_active=true 的"),
    memory_type: str | None = Query(None, description="按类型过滤 (fact/preference/name...)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: int = Depends(get_current_user_id),
):
    """列出当前用户的记忆。"""
    items = await memory_crud_service.list_memories(
        user_id,
        active_only=active_only,
        memory_type=memory_type,
        limit=limit,
        offset=offset,
    )
    return MemoryListResponse(items=[MemoryItem(**it) for it in items], total=len(items))


# =====================================================================
# GET /memories/{memory_id}
# =====================================================================

@router.get("/{memory_id}", response_model=MemoryItem)
async def get_memory(
    memory_id: str,
    user_id: int = Depends(get_current_user_id),
):
    """单条查询。带 user_id 归属校验，防止越权。"""
    item = await memory_crud_service.get_memory(user_id, memory_id)
    if not item:
        raise HTTPException(status_code=404, detail="memory not found or not owned")
    return MemoryItem(**item)


# =====================================================================
# DELETE /memories/{memory_id}
# =====================================================================

@router.delete("/{memory_id}", response_model=MemoryDeleteResponse)
async def delete_memory(
    memory_id: str,
    user_id: int = Depends(get_current_user_id),
):
    """
    软删 + 级联清理：
      PG  is_active=false, deleted_at=now()
      Qdrant  payload.is_active=false
      Redis  清掉 user:{id}:memories 缓存

    返回各层执行状态（pg=Qdrant=Redis 各自独立 try/except）。
    """
    result = await memory_crud_service.delete_memory(user_id, memory_id)
    if not result["pg"]:
        # PG 都没删成功（不存在 / 归属不对 / 已是软删）
        raise HTTPException(
            status_code=404,
            detail="memory not found, not owned, or already deleted",
        )
    return MemoryDeleteResponse(**result)


# =====================================================================
# PATCH /memories/{memory_id}
# =====================================================================

@router.patch("/{memory_id}", response_model=MemoryUpdateResponse)
async def update_memory(
    memory_id: str,
    body: MemoryUpdateRequest,
    user_id: int = Depends(get_current_user_id),
):
    """
    版本化修改：
      - 新 content / importance → 正常 INSERT 新 memory_id（带 merged_into=老行_id 方向）
      - 老行 is_active=false（被新版本替代）
      - Qdrant 老点 deactivate，新点 upsert
      - Redis 清缓存

    返回新 memory_id（None 表示无变化或冲突）。
    """
    new_id = await memory_crud_service.update_memory(
        user_id,
        memory_id,
        new_content=body.new_content,
        new_importance=body.new_importance,
    )
    if new_id is None:
        # 可能：没改任何字段 / 改后 content 跟已存在重复
        raise HTTPException(
            status_code=409,
            detail="no change or conflict (e.g. new content duplicates existing memory)",
        )
    return MemoryUpdateResponse(
        new_memory_id=new_id,
        message="memory updated via versioned insert + deactivate old",
    )


# =====================================================================
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

@router.post("/retrieve", response_model=MemoryRetrieveResponse)
async def retrieve_memories(
    body: MemoryRetrieveRequest,
    user_id: int = Depends(get_current_user_id),
):
    """
    语义检索当前用户的长期记忆（产品核心价值）。

    链路：query → embedding(LM Studio) → Qdrant(user_id + memory_type + score 阈值)
         → 按 memory_id 批量回 PG 取 content（单条 ANY($1) query）。

    - 返回按 cosine 相似度降序排序的命中列表；
    - 命中但已被软删/越权的会自动跳过；
    - Qdrant 不可用或 embedding 失败时优雅返回空（PG 权威不受影响）。
    """
    results = await memory_retrieval_service.retrieve(
        user_id,
        body.query,
        memory_types=body.memory_types,
        top_k=body.top_k,
        score_threshold=body.score_threshold,
        record_access=body.record_access,
    )
    return MemoryRetrieveResponse(
        query=body.query,
        count=len(results),
        results=[MemoryRetrieveItem(**r) for r in results],
    )
