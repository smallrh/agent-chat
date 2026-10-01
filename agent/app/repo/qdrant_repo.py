import asyncio
import logging

from qdrant_client import AsyncQdrantClient, models

from app.core.config import settings

logger = logging.getLogger(__name__)


# 全局单例：整个进程共享同一个 Qdrant 连接。
# 避免 memory_worker / reconcile_consumer / memory_crud_service 各自 new 一个实例、
# 各自连接、各自卡在 None（启动期连不上时谁都没人去重连）的问题。
_qdrant_repo_singleton: "QdrantRepository | None" = None
_qdrant_repo_lock = asyncio.Lock()


def get_qdrant_repo() -> "QdrantRepository":
    """获取（懒创建）QdrantRepository 单例。__init__ 无 I/O，可同步调用。"""
    global _qdrant_repo_singleton
    if _qdrant_repo_singleton is None:
        _qdrant_repo_singleton = QdrantRepository()
    return _qdrant_repo_singleton


class QdrantRepository:
    """
    Qdrant 向量检索（Memory 语义索引）。

    设计原则：Qdrant 只做**检索索引**，不存事实数据。
      - id        = memory_id（UUID str）—— 对应 PG.memories.memory_id
      - vector    = text-embedding
      - payload   = {user_id, memory_type, importance, is_active}
                    ↑ is_active 用于过滤被合并/废弃的僵尸向量

    完整 memory content 永远只在 PostgreSQL 里，按 memory_id join 回来。
    """

    def __init__(self):
        self.client: AsyncQdrantClient | None = None
        self._collection: str = settings.QDRANT_COLLECTION_LTM
        self._collection_ready: bool = False
        self._lock = asyncio.Lock()
        # 防止 start() 被多处（worker / reconcile / crud）重复触发时反复重试/重连
        self._started: bool = False

    async def start(self):
        """
        启动时连接 Qdrant。非致命：连接失败不阻塞主服务（PG 仍是权威存储）。

        容错设计（解决「启动期 Qdrant 未就绪 → 之后一直连不上」）：
          - 启动期短时重试 QDRANT_STARTUP_MAX_RETRIES 次，兼容 docker-compose
            启动顺序（Qdrant 容器可能比 app 晚几秒就绪）。
          - 仍连不上也不致命；后台 _qdrant_health_loop 每 QDRANT_HEALTH_INTERVAL_SECONDS
            秒探活一次，Qdrant 一旦可用会自动重连（含中途重启场景）。
          - 任何实际 Qdrant 操作（upsert/search）也会经 ensure_connected 触发重连。
        """
        if self._started:
            return
        self._started = True
        for attempt in range(1, settings.QDRANT_STARTUP_MAX_RETRIES + 1):
            try:
                await self._connect()
                logger.info(
                    "QdrantRepository started (%s:%s) on attempt %d/%d",
                    settings.QDRANT_HOST, settings.QDRANT_PORT,
                    attempt, settings.QDRANT_STARTUP_MAX_RETRIES,
                )
                return
            except Exception as e:
                logger.warning(
                    "QdrantRepository.start attempt %d/%d failed: %s; retry in %.1fs",
                    attempt, settings.QDRANT_STARTUP_MAX_RETRIES,
                    type(e).__name__ + ":" + str(e)[:120],
                    settings.QDRANT_STARTUP_RETRY_INTERVAL,
                )
                if attempt < settings.QDRANT_STARTUP_MAX_RETRIES:
                    await asyncio.sleep(settings.QDRANT_STARTUP_RETRY_INTERVAL)
        logger.warning(
            "QdrantRepository.start: Qdrant not reachable at %s:%s after %d attempts; "
            "background health loop will keep retrying. "
            "Start Qdrant with: docker run -p 6333:6333 qdrant/qdrant",
            settings.QDRANT_HOST, settings.QDRANT_PORT,
            settings.QDRANT_STARTUP_MAX_RETRIES,
        )
        self.client = None

    async def _connect(self):
        """建立（或重建）到 Qdrant 的连接并探测连通性。失败抛异常由调用方处理。"""
        # 先关掉旧 client，避免重复 new 造成连接泄漏
        old = self.client
        self.client = None
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        client = AsyncQdrantClient(
            host=settings.QDRANT_HOST,
            port=settings.QDRANT_PORT,
            prefer_grpc=False,  # 明确走 REST（容器只暴露 6333，未暴露 gRPC 6334）
        )
        # 探测连通性，失败抛异常
        await client.get_collections()
        self.client = client
        # 连上后下次 upsert 会按真实向量维度（re）建/校验 collection
        self._collection_ready = False
        logger.info("QdrantRepository connected (%s:%s)", settings.QDRANT_HOST, settings.QDRANT_PORT)

    async def ensure_connected(self) -> bool:
        """
        确保 client 可用（带探活 + 惰性重连）。

        返回 True=当前可用；False=本次仍不可用（主链路 PG 不受影响）。
        被所有 Qdrant 操作（upsert/search）和后台健康循环共用，
        这样就连「启动时 Qdrant 没就绪 / 中途重启」也能自动恢复，
        不必等某个业务操作来触发。
        """
        # 已有 client：先做个轻量探活，发现断了就丢弃，下一轮重连
        if self.client is not None:
            try:
                await self.client.get_collections()
                return True
            except Exception:
                logger.warning(
                    "Qdrant liveness failed (%s:%s); will reconnect",
                    settings.QDRANT_HOST, settings.QDRANT_PORT,
                )
                try:
                    await self.client.close()
                except Exception:
                    pass
                self.client = None
                self._collection_ready = False
        # 需要（重）连
        try:
            async with self._lock:
                if self.client is None:
                    await self._connect()
            return self.client is not None
        except Exception:
            logger.debug(
                "Qdrant still not reachable at %s:%s; skip this op, retry later",
                settings.QDRANT_HOST, settings.QDRANT_PORT,
            )
            return False

    async def _ensure_client(self) -> bool:
        """向后兼容：操作前确保 client 可用（委托给 ensure_connected）。"""
        return await self.ensure_connected()

    async def stop(self):
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
            self.client = None
        self._collection_ready = False
        self._started = False
        logger.info("QdrantRepository stopped")

    # ------------------------------------------------------------------
    # 内部：确保 collection 存在
    # ------------------------------------------------------------------

    async def _ensure_collection(self, vector_size: int):
        """第一次 upsert 时用真实 embedding 维度创建 collection。"""
        if not self.client or self._collection_ready:
            return
        exists = await self.client.collection_exists(self._collection)
        if not exists:
            await self.client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=vector_size,
                    distance=models.Distance.COSINE,
                ),
            )
            logger.info(
                "Qdrant: created collection=%s vector_size=%d distance=COSINE",
                self._collection, vector_size,
            )
        self._collection_ready = True

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def upsert(
        self,
        memory_id: str,
        vector: list[float],
        *,
        user_id: int,
        memory_type: str,
        importance: float = 0.5,
        is_active: bool = True,
        slot: str | None = None,
    ) -> bool:
        """
        把一条 memory 的向量写入 Qdrant（或更新）。

        payload 只存检索需要的元数据，不存 content。
        is_active=false 用于标记"被合并到其他记忆"的僵尸向量。
        slot：属性槽位（同槽位多版本只留最新），可选。
        """
        if not await self._ensure_client():
            logger.debug("Qdrant upsert skipped: client not ready")
            return False

        await self._ensure_collection(len(vector))

        await self.client.upsert(
            collection_name=self._collection,
            points=[
                models.PointStruct(
                    id=memory_id,
                    vector=vector,
                    payload={
                        "user_id": user_id,
                        "memory_type": memory_type,
                        "importance": importance,
                        "is_active": is_active,
                        **({"slot": slot} if slot else {}),
                    },
                )
            ],
        )
        logger.info(
            "Qdrant upsert: memory_id=%s user_id=%s type=%s vector_dim=%d is_active=%s",
            memory_id, user_id, memory_type, len(vector), is_active,
        )
        return True

    async def search(
        self,
        user_id: int,
        query_vector: list[float],
        limit: int = 10,
        min_score: float = 0.35,
        memory_types: list[str] | None = None,
    ) -> list[dict]:
        """
        语义召回某个用户的 Memory。

        只用 is_active=true 的向量（被合并/废弃的过滤掉）。
        memory_types 可选：只在指定的记忆类型里召回（如 ['preference','name']）。
        返回 list[{memory_id, score, payload}]。
        """
        if not await self._ensure_client() or not self._collection_ready:
            return []

        filter_must = [
            models.FieldCondition(
                key="user_id",
                match=models.MatchValue(value=user_id),
            ),
            models.FieldCondition(
                key="is_active",
                match=models.MatchValue(value=True),
            ),
        ]
        # 类型过滤：单类型用 MatchValue，多类型用 MatchAny
        if memory_types:
            if len(memory_types) == 1:
                filter_must.append(
                    models.FieldCondition(
                        key="memory_type",
                        match=models.MatchValue(value=memory_types[0]),
                    )
                )
            else:
                filter_must.append(
                    models.FieldCondition(
                        key="memory_type",
                        match=models.MatchAny(values=memory_types),
                    )
                )

        results = await self.client.query_points(
            collection_name=self._collection,
            query=query_vector,
            limit=limit,
            score_threshold=min_score,
            with_payload=True,
            query_filter=models.Filter(must=filter_must),
        )

        hits = []
        for point in results.points:
            hits.append({
                "memory_id": point.id,
                "score": point.score,
                "payload": point.payload or {},
            })
        logger.info(
            "Qdrant search: user_id=%d query_dim=%d hits=%d",
            user_id, len(query_vector), len(hits),
        )
        return hits

    # ------------------------------------------------------------------
    # 近似合并：同类型语义相似检测
    # ------------------------------------------------------------------

    async def find_similar_in_type(
        self,
        user_id: int,
        memory_type: str,
        vector: list[float],
        threshold: float | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """
        在同 user + 同 memory_type 范围内，找 cosine 相似度 ≥ threshold 的
        已落库记忆（is_active=true），用于近似合并。

        Args:
            threshold: cosine 下限，默认 MEMORY_MERGE_SIMILARITY_THRESHOLD (0.85)。
                       0.80~0.85 推荐；0.90+ 极严格。
            limit: 最多返回几个候选。

        Returns:
            list[dict] 按 score 降序，每个 {memory_id, score}。
                       空列表表示没找到相似。

        注意：
            - 只在同类型内找，跨类型不合并（preference 和 name 不可能同义）
            - 只找 is_active=true 的，避免把被合并过的又拉回来
            - 找到的第一个（score 最高）就是最像的，通常就用它做合并目标
        """
        if not await self._ensure_client() or not self._collection_ready:
            return []

        threshold = threshold or settings.MEMORY_MERGE_VECTOR_THRESHOLD

        results = await self.client.query_points(
            collection_name=self._collection,
            query=vector,
            limit=limit,
            score_threshold=threshold,
            with_payload=False,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id",
                        match=models.MatchValue(value=user_id),
                    ),
                    models.FieldCondition(
                        key="memory_type",
                        match=models.MatchValue(value=memory_type),
                    ),
                    models.FieldCondition(
                        key="is_active",
                        match=models.MatchValue(value=True),
                    ),
                ]
            ),
        )

        hits = [
            {"memory_id": str(p.id), "score": p.score}
            for p in results.points
        ]
        if hits:
            logger.info(
                "Qdrant find_similar: user_id=%d type=%s threshold=%.2f "
                "candidates=%d top=%.4f",
                user_id, memory_type, threshold, len(hits), hits[0]["score"],
            )
        return hits

    async def deactivate(self, memory_id: str):
        """
        把一条 Qdrant 向量标记为 is_active=false（被合并时调用）。
        只是更新 payload，不删点——保留数据便于溯源。
        """
        if not await self._ensure_client():
            return
        try:
            await self.client.upsert(
                collection_name=self._collection,
                points=[
                    models.PointStruct(
                        id=memory_id,
                        vector=[0.0] * 4,  # Qdrant 要求 upsert 带 vector；用占位，实际被 filter 跳过
                        payload={"is_active": False},
                    )
                ],
            )
        except Exception:
            # 有些场景（比如老向量没 is_active payload 字段）会 upsert 失败
            # 降级：删掉这个点
            logger.warning(
                "Qdrant deactivate fallback to delete: memory_id=%s",
                memory_id,
            )
            try:
                await self.client.delete(
                    collection_name=self._collection,
                    points_selector=models.PointIdsList(points=[memory_id]),
                )
            except Exception:
                logger.exception("Qdrant deactivate delete also failed: memory_id=%s", memory_id)

    async def hard_delete(self, memory_id: str):
        """
        彻底删掉 Qdrant 点（CRUD API DELETE 场景用，可选）。
        注意：deactivate（payload.is_active=false）已足够让向量不出现在检索结果里。
        hard_delete 只是额外省点存储，失败不影响查询正确性。
        """
        if not await self._ensure_client():
            return
        try:
            await self.client.delete(
                collection_name=self._collection,
                points_selector=models.PointIdsList(points=[memory_id]),
            )
            logger.info("Qdrant hard_delete: memory_id=%s", memory_id)
        except Exception:
            logger.exception("Qdrant hard_delete failed (ignored): memory_id=%s", memory_id)
