"""Memory 相关服务。

两个类并存，职责清晰：
  - MemoryService          = embedding 封装（LM Studio），被 reconcile_consumer 复用
  - MemoryCRUDService      = 对外 CRUD API（list / delete / update 版本化），新增给 memory API 路由用
"""
import logging
import uuid as _uuid

from openai import AsyncOpenAI

from app.core.config import settings
from app.repo.memory_repo import MemoryRepository
from app.repo.qdrant_repo import get_qdrant_repo
from app.repo.redis_repo import RedisRepository

logger = logging.getLogger(__name__)


# =====================================================================
# MemoryService  — embedding 封装（被 reconcile_consumer 复用）
# 构造签名必须兼容：MemoryService(redis_repo, memory_repo, qdrant_repo)
# =====================================================================

class MemoryService:
    """
    长期记忆服务（被 reconcile_consumer 复用的轻量版本）。

    只封装 embedder（LM Studio），构造参数里的 repo 目前没用，
    留着是为了跟 memory_worker.MemoryService 构造签名对齐。
    """

    def __init__(
        self,
        redis_repo: RedisRepository | None = None,
        memory_repo: MemoryRepository | None = None,
        qdrant_repo: QdrantRepository | None = None,
    ):
        self.redis_repo = redis_repo
        self.memory_repo = memory_repo
        self.qdrant_repo = qdrant_repo

        # Embedding（LM Studio 本地 nomic-embed-text，OpenAI 兼容）
        self.embedder = AsyncOpenAI(
            api_key=settings.EMBEDDING_API_KEY,
            base_url=settings.EMBEDDING_BASE_URL,
        )

    async def embed(self, text: str) -> list[float] | None:
        """
        调 LM Studio 本地 nomic-embed-text 拿向量。
        失败返回 None（上层 graceful skip Qdrant）。
        """
        try:
            r = await self.embedder.embeddings.create(
                model=settings.EMBEDDING_MODEL,
                input=text[:2000],
            )
            vec = r.data[0].embedding
            logger.info(
                "embed: dim=%d model=%s text=%.40s",
                len(vec), settings.EMBEDDING_MODEL, text,
            )
            return vec
        except Exception:
            logger.exception("embed failed (LM Studio unreachable?): text=%.60s", text)
            return None


# =====================================================================
# MemoryCRUDService  — 对外 CRUD API
# =====================================================================

class MemoryCRUDService:
    """Memory CRUD：查看 / 软删 / 级联清理。"""

    def __init__(self):
        self.memory_repo = MemoryRepository()
        self.qdrant_repo = get_qdrant_repo()
        self.redis_repo = RedisRepository()
        self._started = False

    async def start(self):
        """懒启动（路由第一次调用时触发）。"""
        if self._started:
            return
        await self.memory_repo.start()
        await self.qdrant_repo.start()
        await self.redis_repo.start()
        self._started = True
        logger.info("MemoryCRUDService started")

    async def stop(self):
        if not self._started:
            return
        await self.redis_repo.stop()
        await self.qdrant_repo.stop()
        await self.memory_repo.stop()
        self._started = False
        logger.info("MemoryCRUDService stopped")

    # ------------------------------------------------------------------
    # GET /memories
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
        """列出用户的记忆列表（CRUD 核心）。"""
        if not self._started:
            await self.start()
        return await self.memory_repo.list_memories(
            user_id, active_only=active_only, memory_type=memory_type,
            limit=limit, offset=offset,
        )

    async def get_memory(self, user_id: int, memory_id: str) -> dict | None:
        """单条查询。带 user_id 校验防越权。"""
        if not self._started:
            await self.start()
        try:
            row = await self.memory_repo.get_by_id(memory_id)
        except Exception:
            return None
        if not row or int(row["user_id"]) != user_id:
            return None
        # 把 UUID / datetime / jsonb 转成 JSON-safe
        import json as _json
        d = dict(row)
        d["memory_id"] = str(d["memory_id"])
        if d.get("conversation_id"):
            d["conversation_id"] = str(d["conversation_id"])
        if d.get("merged_into"):
            d["merged_into"] = str(d["merged_into"])
        for ts in ("created_at", "updated_at", "synced_at"):
            v = d.get(ts)
            if v is not None:
                d[ts] = v.isoformat()
        if d.get("source_message_ids"):
            try:
                d["source_message_ids"] = _json.loads(d["source_message_ids"]) \
                    if isinstance(d["source_message_ids"], str) else d["source_message_ids"]
            except Exception:
                pass
        return d

    # ------------------------------------------------------------------
    # DELETE /memories/{id}  — 软删 + PG/Qdrant/Redis 级联
    # ------------------------------------------------------------------

    async def delete_memory(self, user_id: int, memory_id: str) -> dict:
        """
        软删 + 级联清理：
          1) PG soft_delete           ← 权威
          2) Qdrant deactivate         ← payload.is_active=false
          3) Redis clear_user_cache    ← 清掉 List，下次重新查 PG

        返回: {"pg": bool, "qdrant": bool, "redis": bool} 各层状态
        """
        if not self._started:
            await self.start()

        result = {"pg": False, "qdrant": False, "redis": False}

        # 先校验归属
        existing = await self.memory_repo.get_by_id(memory_id)
        if not existing or int(existing["user_id"]) != user_id:
            logger.warning("delete_memory: memory_id=%s not found or not owned by user %d",
                          memory_id, user_id)
            return result

        # 1) PG 软删
        try:
            result["pg"] = await self.memory_repo.soft_delete(memory_id)
        except Exception:
            logger.exception("delete_memory: PG soft_delete failed memory_id=%s", memory_id)

        # 2) Qdrant deactivate（失败不阻塞）
        try:
            await self.qdrant_repo.deactivate(memory_id)
            result["qdrant"] = True
        except Exception:
            logger.exception("delete_memory: Qdrant deactivate failed memory_id=%s", memory_id)

        # 3) Redis 清用户缓存（下次 chat 会重新拉 PG）
        try:
            await self.redis_repo.clear_memories(user_id)
            result["redis"] = True
        except Exception:
            logger.exception("delete_memory: Redis clear_memories failed user_id=%d", user_id)

        logger.info(
            "MemoryCRUDService.delete_memory: memory_id=%s user_id=%d "
            "pg=%s qdrant=%s redis=%s",
            memory_id, user_id, result["pg"], result["qdrant"], result["redis"],
        )
        return result

    # ------------------------------------------------------------------
    # PATCH /memories/{id} — 版本化修改（新 INSERT + 老行 is_active=false）
    # ------------------------------------------------------------------

    async def update_memory(
        self,
        user_id: int,
        memory_id: str,
        *,
        new_content: str | None = None,
        new_importance: float | None = None,
    ) -> str | None:
        """
        版本化修改：
          - 老行 is_active=false, merged_into=新行_id
          - 新行正常 INSERT（content 改后走精确去重）
          - Qdrant 老点 deactivate，新点 upsert

        返回新 memory_id（或 None=没改/冲突）。
        """
        if not self._started:
            await self.start()

        existing = await self.memory_repo.get_by_id(memory_id)
        if not existing or int(existing["user_id"]) != user_id:
            logger.warning("update_memory: memory_id=%s not found or not owned by user %d",
                          memory_id, user_id)
            return None

        # 没改任何字段
        if not new_content and new_importance is None:
            logger.info("update_memory: no-op memory_id=%s", memory_id)
            return None

        from app.kafka.memory_worker import _EXTRACTION_PROMPT_VERSION

        new_memory = {
            "memory_type": existing["memory_type"],
            "content": new_content if new_content else existing["content"],
            "importance": new_importance if new_importance is not None else existing["importance"],
            "slot": existing.get("slot"),   # 同槽位收敛：新版本继承旧版本的 slot
            # 溯源：版本化也带来源信息，新 content 带 updated_at
            "embedding_model": existing.get("embedding_model"),
            "source_message_ids": existing.get("source_message_ids") or [],
            "extract_model": existing.get("extract_model"),
            "prompt_version": _EXTRACTION_PROMPT_VERSION,
        }

        new_id = await self.memory_repo.save(user_id, new_memory)
        if not new_id:
            logger.info("update_memory: SKIP (PG conflict - 改后的 content 和已存在重复)")
            return None

        # 老行标被新行替代
        await self.memory_repo.mark_merged(memory_id, new_id)

        # Qdrant 老点 deactivate
        try:
            await self.qdrant_repo.deactivate(memory_id)
        except Exception:
            logger.exception("update_memory: Qdrant deactivate old failed")

        # Redis 清缓存
        try:
            await self.redis_repo.clear_memories(user_id)
        except Exception:
            logger.exception("update_memory: Redis clear failed")

        logger.info(
            "MemoryCRUDService.update_memory: old=%s → new=%s user_id=%d",
            memory_id, new_id, user_id,
        )
        return new_id


# 单例（和 chat_service 同模式）
memory_crud_service = MemoryCRUDService()
