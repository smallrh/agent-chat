import json
import uuid as _uuid
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from app.core.config import settings

logger = logging.getLogger(__name__)


def _content_hash(content: str) -> str:
    """
    归一化 + sha256 指纹，用于去重锚点。

    strip() 对齐 Python / PG 两边的空白差异，避免 LLM 前后偶发换行导致"同一内容"被误判为不同。
    """
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def _next_backoff(retry_count: int) -> timedelta:
    """
    指数退避：1min → 2min → 4min → ... 上限 24h。

    retry_count=0 → 1min, retry_count=1 → 2min, retry_count=2 → 4min, ...
    """
    base = min(2 ** retry_count, 24 * 60)  # 上限 24h（以 minute 为单位）
    return timedelta(minutes=base)


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "…(truncated)"


class MemoryRepository:
    """
    长期记忆持久化（PostgreSQL）。

    写入 memories 表，返回新行的 memory_id（UUID）。
    Qdrant 用这个 memory_id 做向量主键（不把 content 塞进 Qdrant）。
    """

    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def start(self):
        self.pool = await asyncpg.create_pool(
            host=settings.PG_HOST,
            port=settings.PG_PORT,
            database=settings.PG_DATABASE,
            user=settings.PG_USER,
            password=settings.PG_PASSWORD,
            min_size=1,
            max_size=settings.PG_POOL_SIZE,
        )
        logger.info("MemoryRepository started (PostgreSQL pool)")

    async def stop(self):
        if self.pool:
            await self.pool.close()
            self.pool = None
        logger.info("MemoryRepository stopped")

    async def save(self, user_id: int, memory: dict) -> str | None:
        """
        把一条 memory 写进 memories 表，返回 memory_id（UUID str）。

        幂等由 PG UNIQUE 约束 (user_id, memory_type, content_hash) 原子保证：
        并发同内容只落一行，冲突时 RETURNING 返回 None，上层安全短路。

        memory 字段约定（来自 LLM extract）：
            {
                "memory_type": "preference",
                "content": "用户喜欢辣的食物",
                "importance": 0.8,
                "source_conversation_id": "...",
            }
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        content = memory["content"]
        mtype = memory["memory_type"]
        importance = memory.get("importance", 0.5)
        slot = memory.get("slot")  # 属性槽位：同槽位多版本只留最新（冲突收敛）
        source_conv_id = memory.get("source_conversation_id")
        memory_id = str(_uuid.uuid4())
        content_hash = _content_hash(content)

        # 溯源字段（extract() 注入的；None 时 PG 列有 DEFAULT/允许 NULL）
        source_msg_ids = memory.get("source_message_ids") or []
        extract_model = memory.get("extract_model")
        prompt_version = memory.get("prompt_version")
        embedding_model = memory.get("embedding_model")

        async with self.pool.acquire() as conn:
            # ON CONFLICT DO NOTHING + RETURNING：
            #   - 命中 UNIQUE → 不插，RETURNING 列空 → row=None → 返回 None
            #   - 新行       → 插，RETURNING memory_id → 返回
            # 这一条 SQL 同时解决"前置 SELECT 竞态"问题
            row = await conn.fetchrow(
                """
                INSERT INTO memories (
                    memory_id,
                    user_id,
                    conversation_id,
                    memory_type,
                    slot,
                    content,
                    content_hash,
                    importance,
                    confidence,
                    metadata,
                    sync_status,
                    sync_retry_count,
                    source_message_ids,
                    extract_model,
                    prompt_version,
                    embedding_model
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'pending', 0,
                    $11::jsonb, $12, $13, $14
                )
                ON CONFLICT (user_id, memory_type, content_hash) DO NOTHING
                RETURNING memory_id
                """,
                memory_id,
                user_id,
                _uuid.UUID(source_conv_id) if source_conv_id else None,
                mtype,
                slot,
                content,
                content_hash,
                importance,
                memory.get("confidence", 0.8),
                memory.get("metadata"),
                json.dumps(source_msg_ids),
                extract_model,
                prompt_version,
                embedding_model,
            )

        if row is None:
            logger.info(
                "MemoryRepository.save: SKIP on conflict "
                "(user_id=%s, type=%s, content_hash=%s, content=%.50s)",
                user_id, mtype, content_hash, content,
            )
            return None

        result_id = str(row["memory_id"])
        logger.info(
            "MemoryRepository.save: OK memory_id=%s user_id=%s type=%s content=%.50s",
            result_id, user_id, mtype, content,
        )
        return result_id

    # ------------------------------------------------------------------
    # sync 状态：孤儿记忆修复
    # ------------------------------------------------------------------

    async def get_by_id(self, memory_id: str) -> dict | None:
        """按 memory_id 拉单行。全列 SELECT，供 CRUD API 和合并精判共用。"""
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT memory_id, user_id, conversation_id, memory_type, slot, content, importance, "
                "confidence, metadata, sync_status, sync_retry_count, sync_error, "
                "synced_at, source_message_ids, extract_model, prompt_version, "
                "embedding_model, is_active, merged_into, deleted_at, "
                "created_at, updated_at "
                "FROM memories WHERE memory_id=$1",
                _uuid.UUID(memory_id),
            )
        if not row:
            return None
        return dict(row)

    async def get_by_ids(
        self,
        memory_ids: list[str],
        *,
        user_id: int | None = None,
    ) -> dict[str, dict]:
        """
        检索链路"回表"：按 memory_id 批量取 content（单条 PG query）。

        用 `WHERE memory_id = ANY($1::uuid[])` 一次性拿回所有命中的行，
        避免"每条命中各查一次 PG"造成的 N 次往返。

        可选兜底：
          - user_id：叠加归属校验，防 Qdrant 返回越权 id
          - is_active=TRUE：过滤掉已被软删/合并的僵尸记忆
            （极小概率 Qdrant 索引与 PG 状态存在时间差）

        返回 {memory_id(str): normalized_row(dict)}，方便上层按 Qdrant 的
        score 顺序重新排列（批量查询本身不保证顺序）。

        normalization：UUID→str、jsonb→dict、timestamp→ISO，
        直接可 JSON 序列化（供 API / 内部 RAG 共用）。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        if not memory_ids:
            return {}

        uuids = [_uuid.UUID(m) for m in memory_ids]

        conditions = ["memory_id = ANY($1::uuid[])"]
        params: list = [uuids]
        if user_id is not None:
            params.append(user_id)
            conditions.append(f"user_id = ${len(params)}")
            params.append(True)
            conditions.append(f"is_active = ${len(params)}")

        sql = (
            "SELECT memory_id, user_id, conversation_id, memory_type, slot, content, "
            "importance, confidence, metadata, sync_status, is_active, "
            "merged_into, created_at, updated_at "
            f"FROM memories WHERE {' AND '.join(conditions)}"
        )

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        result: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            d["memory_id"] = str(d["memory_id"])
            if d.get("conversation_id"):
                d["conversation_id"] = str(d["conversation_id"])
            if d.get("merged_into"):
                d["merged_into"] = str(d["merged_into"])
            if d.get("metadata"):
                try:
                    d["metadata"] = json.loads(d["metadata"]) \
                        if isinstance(d["metadata"], str) else d["metadata"]
                except Exception:
                    pass
            for ts in ("created_at", "updated_at"):
                v = d.get(ts)
                if v is not None:
                    d[ts] = v.isoformat()
            result[d["memory_id"]] = d
        logger.info(
            "MemoryRepository.get_by_ids: requested=%d returned=%d",
            len(memory_ids), len(result),
        )
        return result

    async def record_access(self, memory_ids: list[str]) -> None:
        """
        批量更新访问计数（检索"读取端"的可观测性）。

        独立 write，不计入上面的"回表" read——保持检索主链路"只回 PG 一次"。
        命中即 +1 access_count，并更新 last_accessed_at。
        """
        if not self.pool or not memory_ids:
            return
        uuids = [_uuid.UUID(m) for m in memory_ids]
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories "
                "SET access_count = access_count + 1, last_accessed_at = now() "
                "WHERE memory_id = ANY($1::uuid[])",
                uuids,
            )

    async def mark_synced(self, memory_id: str):
        """
        embed + Qdrant 成功后标记 indexed。
        清掉 retry_count / sync_error / retry_after，synced_at = now()。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE memories
                SET sync_status     = 'indexed',
                    sync_retry_count = 0,
                    sync_error       = NULL,
                    retry_after      = NULL,
                    synced_at        = now()
                WHERE memory_id = $1
                """,
                _uuid.UUID(memory_id),
            )
        logger.info("MemoryRepository.mark_synced: memory_id=%s", memory_id)

    async def mark_pending_failed(self, memory_id: str, error: str):
        """
        embed 或 Qdrant 失败后，保持 pending，累加 retry_count，
        记录错误原因，计算下次重试时间（指数退避）。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        err_trunc = _truncate(error)

        # 先拿当前 retry_count 算下一次 backoff
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT sync_retry_count FROM memories WHERE memory_id = $1",
                _uuid.UUID(memory_id),
            )
            current_retries = row["sync_retry_count"] if row else 0
            next_count = current_retries + 1
            backoff = _next_backoff(current_retries)
            retry_after = datetime.now(timezone.utc) + backoff

            await conn.execute(
                """
                UPDATE memories
                SET sync_status     = 'pending',
                    sync_retry_count = $1,
                    sync_error       = $2,
                    retry_after      = $3,
                    synced_at        = now()
                WHERE memory_id = $4
                """,
                next_count,
                err_trunc,
                retry_after,
                _uuid.UUID(memory_id),
            )
        logger.warning(
            "MemoryRepository.mark_pending_failed: memory_id=%s retry_count=%d "
            "retry_after=%s error=%.120s",
            memory_id, next_count, retry_after.isoformat(), err_trunc,
        )

    async def list_pending(self, limit: int = 100) -> list[dict]:
        """
        取出所有 sync_status='pending' 且已到重试时间的记忆。

        返回 list[dict]，每条包含：memory_id / user_id / memory_type /
        content / importance / sync_retry_count。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT memory_id, user_id, memory_type, content,
                       importance, sync_retry_count, sync_error
                FROM memories
                WHERE sync_status = 'pending'
                  AND (retry_after IS NULL OR retry_after <= now())
                ORDER BY
                    sync_retry_count ASC,    -- 先跑没怎么重试过的
                    created_at ASC            -- 先跑老的
                LIMIT $1
                """,
                limit,
            )

        return [
            {
                "memory_id": str(r["memory_id"]),
                "user_id": r["user_id"],
                "memory_type": r["memory_type"],
                "content": r["content"],
                "importance": r["importance"],
                "sync_retry_count": r["sync_retry_count"],
                "sync_error": r["sync_error"],
            }
            for r in rows
        ]

    async def count_pending(self) -> int:
        """当前 pending 总数（对账日志用）。"""
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM memories WHERE sync_status = 'pending'"
            )

    # ------------------------------------------------------------------
    # 近似合并：标记"被合并到哪条记忆"
    # ------------------------------------------------------------------

    async def mark_merged(self, old_id: str, keep_id: str):
        """
        把 old_id 标记为被合并到 keep_id。

        PG: is_active=false, merged_into=keep_id
        Qdrant: 不动（payload filter is_active=true 自然过滤）

        Args:
            old_id: 被合并掉的那条（新写入）
            keep_id: 保留的那条（已落库）
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE memories
                SET is_active  = false,
                    merged_into = $2,
                    updated_at  = now()
                WHERE memory_id = $1
                """,
                _uuid.UUID(old_id),
                _uuid.UUID(keep_id),
            )
        logger.info(
            "MemoryRepository.mark_merged: old=%s merged_into keep=%s",
            old_id, keep_id,
        )

    async def set_active(self, memory_id: str, is_active: bool):
        """辅助：手动切换 is_active（对账 / 运维脚本用）。"""
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET is_active=$2 WHERE memory_id=$1",
                _uuid.UUID(memory_id), is_active,
            )

    # ------------------------------------------------------------------
    # CRUD：查看 / 软删 / 硬删
    # ------------------------------------------------------------------

    async def list_memories(
        self,
        user_id: int,
        *,
        active_only: bool = True,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        列出某个用户的 memory（CRUD API 用）。

        默认只返回 is_active=true 的；active_only=False 时把被删/被合并的也带出来。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        conditions = ["user_id = $1"]
        params: list = [user_id]
        if active_only:
            conditions.append("is_active = TRUE")
        if memory_type:
            params.append(memory_type)
            conditions.append(f"memory_type = ${len(params) + 1}")

        sql = (
            "SELECT memory_id, conversation_id, memory_type, slot, content, importance, "
            "confidence, metadata, sync_status, sync_retry_count, sync_error, "
            "synced_at, source_message_ids, extract_model, prompt_version, "
            "embedding_model, is_active, merged_into, deleted_at, "
            "created_at, updated_at "
            "FROM memories "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY importance DESC, created_at DESC "
            f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        result = []
        for r in rows:
            d = dict(r)
            # 把 UUID 转成 str（fastapi response_model 需要）
            d["memory_id"] = str(d["memory_id"])
            if d.get("conversation_id"):
                d["conversation_id"] = str(d["conversation_id"])
            if d.get("merged_into"):
                d["merged_into"] = str(d["merged_into"])
            # jsonb 字段：asyncpg 返回 str，json.loads 成 list
            if d.get("source_message_ids"):
                try:
                    d["source_message_ids"] = json.loads(d["source_message_ids"]) \
                        if isinstance(d["source_message_ids"], str) else d["source_message_ids"]
                except Exception:
                    pass
            if d.get("metadata"):
                try:
                    d["metadata"] = json.loads(d["metadata"]) \
                        if isinstance(d["metadata"], str) else d["metadata"]
                except Exception:
                    pass
            # datetime 转 ISO 字符串
            for ts in ("created_at", "updated_at", "synced_at", "deleted_at"):
                v = d.get(ts)
                if v is not None:
                    d[ts] = v.isoformat()
            result.append(d)
        return result

    async def soft_delete(self, memory_id: str) -> bool:
        """
        软删：is_active=false + deleted_at=now()。
        返回 True=删了；False=memory_id 不存在。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE memories
                SET is_active = false,
                    deleted_at  = now(),
                    updated_at  = now()
                WHERE memory_id = $1 AND is_active = true
                """,
                _uuid.UUID(memory_id),
            )
        # asyncpg execute 返回 "UPDATE N" 形式
        affected = int(result.split()[-1]) if result else 0
        logger.info(
            "MemoryRepository.soft_delete: memory_id=%s affected=%d", memory_id, affected,
        )
        return affected > 0

    async def hard_delete(self, memory_id: str) -> bool:
        """硬删（极少用，溯源丢失）。返回 True=删了。"""
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM memories WHERE memory_id = $1",
                _uuid.UUID(memory_id),
            )
        affected = int(result.split()[-1]) if result else 0
        logger.info(
            "MemoryRepository.hard_delete: memory_id=%s affected=%d", memory_id, affected,
        )
        return affected > 0
