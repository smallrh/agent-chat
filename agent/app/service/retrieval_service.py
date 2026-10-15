"""Memory 读取端（产品核心价值）。

检索链路：
  query → embedding(LM Studio nomic-embed-text) → Qdrant(user_id + memory_type + score 阈值)
        → 按 memory_id 批量回 PG 取 content（单条 ANY($1) query）

设计要点：
  - PG 是 content 权威存储；Qdrant 只做语义召回，绝不回 content。
  - 回表只用一次 PG 批量查询（get_by_ids / ANY($1)），并按 Qdrant 的
    score 降序重新排列结果。
  - memory_type 可选过滤（只读某一类记忆，如 preference / name）。
  - 命中顺序 = Qdrant score 顺序（语义相似度高的排前面）。
  - Qdrant 不可用时优雅降级：embedding 失败 / 无命中 → 返回 []，不报错。
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from app.core.config import settings
from app.repo.memory_repo import MemoryRepository
from app.repo.qdrant_repo import get_qdrant_repo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 冲突收敛：同 slot（属性槽位）多版本只保留最新，矛盾记忆不并存
# ---------------------------------------------------------------------------

def _converge_by_slot(memories: list[dict]) -> list[dict]:
    """
    按 slot（属性槽位）收敛：同一 slot 只保留 created_at 最新的一条，
    旧版本 / 矛盾记忆被丢弃，避免"北京 vs 深圳""Java vs Go"同时注入。

    - 排序键：(created_at 降序, score 降序) → 每个 slot 第一个就是"最新值"
    - 有 slot 的：同 slot 只留最新；无 slot 的（老数据/未归类）：各自按 memory_id 独立保留
    - 收敛后再按 score 降序重排，保持相关性排序

    注意：created_at 来自 PG，已规范成 ISO 字符串，字典序即时间序。
    """
    def _sort_key(m: dict):
        return (
            m.get("created_at") or "",   # 空串排最后
            m.get("score") or 0.0,
        )

    ordered = sorted(memories, key=_sort_key, reverse=True)

    best: dict[str, dict] = {}
    for m in ordered:
        slot = m.get("slot")
        if slot:                       # 有槽位 → 同槽位只留最新
            if slot in best:
                continue
            best[slot] = m
        else:                          # 无槽位 → 向后兼容，按 memory_id 去重保留
            key = "noslot:" + str(m.get("memory_id"))
            if key in best:
                continue
            best[key] = m

    result = list(best.values())
    # 还原为 score 降序（相关性优先）
    result.sort(key=lambda m: (m.get("score") or 0.0), reverse=True)
    return result



class RetrievalService:
    """
    长期记忆语义检索（读取端）。

    对外暴露 retrieve()，内部串起 embed → Qdrant.search → PG.get_by_ids 三段。
    """

    def __init__(self):
        self.memory_repo = MemoryRepository()
        self.qdrant_repo = get_qdrant_repo()
        # Embedding（与 MemoryService 同源：LM Studio 本地 nomic-embed-text）
        self.embedder = AsyncOpenAI(
            api_key=settings.EMBEDDING_API_KEY,
            base_url=settings.EMBEDDING_BASE_URL,
        )
        self._started = False

    async def start(self):
        """懒启动（路由第一次调用时触发）。"""
        if self._started:
            return
        await self.memory_repo.start()
        await self.qdrant_repo.start()
        self._started = True
        logger.info("RetrievalService started")

    async def stop(self):
        if not self._started:
            return
        await self.memory_repo.stop()
        self._started = False
        logger.info("RetrievalService stopped")

    async def aclose(self):
        """lifespan shutdown 用的别名（内部资源释放委托给 stop）。"""
        await self.stop()

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def embed(self, text: str) -> list[float] | None:
        """调 LM Studio 拿 query 向量。失败返回 None（上层 graceful 降级）。"""
        try:
            r = await self.embedder.embeddings.create(
                model=settings.EMBEDDING_MODEL,
                input=text[:2000],
            )
            vec = r.data[0].embedding
            logger.info(
                "RetrievalService.embed: dim=%d model=%s text=%.40s",
                len(vec), settings.EMBEDDING_MODEL, text,
            )
            return vec
        except Exception:
            logger.exception("RetrievalService.embed failed: text=%.60s", text)
            return None

    # ------------------------------------------------------------------
    # 检索主链路
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        user_id: int,
        query: str,
        *,
        memory_types: list[str] | None = None,
        top_k: int = 10,
        score_threshold: float | None = None,
        record_access: bool = False,
    ) -> list[dict]:
        """
        语义检索某用户的长期记忆，返回按相似度降序的结果列表。

        Args:
            user_id:        用户内部分（users.id），必填（Qdrant + PG 双校验）。
            query:          自然语言检索语句。
            memory_types:   可选，限定记忆类型；None=全部类型。
            top_k:          最多返回几条（Qdrant limit）。
            score_threshold: cosine 下限；None=用 settings 默认值。
            record_access:  是否顺带 +1 访问计数（独立 write，不计入主链路回表）。

        Returns:
            list[dict]，每个 {
                memory_id, memory_type, content, score,
                importance, confidence, created_at, metadata
            }，score 降序。

        降级：
            - query 空 → []
            - embedding 失败 / Qdrant 不可用 → []
            - 某命中回表缺失（被软删/越权）→ 跳过该条，不影响其它
        """
        if not self._started:
            await self.start()

        query = (query or "").strip()
        if not query:
            return []

        # 1) embedding
        vector = await self.embed(query)
        if vector is None:
            logger.warning(
                "RetrievalService.retrieve: embedding failed, skip Qdrant "
                "(user_id=%d query=%.40s)",
                user_id, query,
            )
            return []

        threshold = (
            score_threshold
            if score_threshold is not None
            else settings.MEMORY_SEARCH_SCORE_THRESHOLD
        )

        # 2) Qdrant 语义召回（user_id + memory_type + score 阈值）
        hits = await self.qdrant_repo.search(
            user_id=user_id,
            query_vector=vector,
            limit=top_k,
            min_score=threshold,
            memory_types=memory_types,
        )
        if not hits:
            logger.info(
                "RetrievalService.retrieve: no Qdrant hits "
                "(user_id=%d query=%.40s threshold=%.2f)",
                user_id, query, threshold,
            )
            return []

        # 去重（双保险）并保持 Qdrant 的 score 降序
        ordered_ids: list[str] = []
        seen: set[str] = set()
        score_by_id: dict[str, float] = {}
        for h in hits:
            mid = h["memory_id"]
            score_by_id[mid] = h.get("score")
            if mid in seen:
                continue
            seen.add(mid)
            ordered_ids.append(mid)

        # 3) 单条 PG 批量回表（按 memory_id 取 content）
        rows_by_id = await self.memory_repo.get_by_ids(ordered_ids, user_id=user_id)

        # 4) 按 score 顺序组装结果（跳过被软删/越权的命中）
        results: list[dict] = []
        for mid in ordered_ids:
            row = rows_by_id.get(mid)
            if not row:
                continue
            results.append({
                "memory_id": mid,
                "memory_type": row.get("memory_type"),
                "slot": row.get("slot"),
                "content": row.get("content"),
                "score": score_by_id.get(mid),
                "importance": row.get("importance"),
                "confidence": row.get("confidence"),
                "created_at": row.get("created_at"),
                "metadata": row.get("metadata"),
            })

        # 冲突收敛：同 slot 多版本只留最新（MEMORY_CONVERGE_BY_SLOT 开关）
        if settings.MEMORY_CONVERGE_BY_SLOT:
            converged = _converge_by_slot(results)
            if len(converged) != len(results):
                logger.info(
                    "RetrievalService.retrieve: slot convergence %d → %d "
                    "(dropped %d stale/conflicting)",
                    len(results), len(converged), len(results) - len(converged),
                )
            results = converged

        # 可选：更新访问计数（独立 write，只统计真正返回的记忆，
        # 不计入"回表"那一次 read，也不为被软删跳过的 id 计数）
        if record_access and results:
            await self.memory_repo.record_access([r["memory_id"] for r in results])

        logger.info(
            "RetrievalService.retrieve: user_id=%d query=%.40s "
            "hits=%d returned=%d threshold=%.2f",
            user_id, query, len(hits), len(results), threshold,
        )
        return results


# 单例（和 memory_crud_service / chat_service 同模式）
memory_retrieval_service = RetrievalService()
