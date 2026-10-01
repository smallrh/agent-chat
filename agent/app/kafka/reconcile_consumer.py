"""
孤儿记忆对账 Consumer。

独立 Kafka consumer，监听 memory.reconcile topic。
收到触发消息后，从 PG 拉 sync_status='pending' 且到期可重试的记忆，
重新跑 embed + Qdrant，成功则标记 indexed，失败则累加 backoff。

与 MemoryWorkerConsumer 平行：
  - MemoryWorkerConsumer  → memory.tasks   → 正常写入链路
  - ReconcileConsumer      → memory.reconcile → 孤儿修复链路
"""

import asyncio
import json
import logging

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import CommitFailedError

from app.core.config import settings
from app.repo.memory_repo import MemoryRepository
from app.repo.qdrant_repo import get_qdrant_repo
from app.service.memory_service import MemoryService
from app.repo.redis_repo import RedisRepository

logger = logging.getLogger(__name__)


class ReconcileConsumer:
    """
    消费 memory.reconcile topic，周期性补齐 PG 有但 Qdrant 无的记忆。
    """

    def __init__(self):
        self.consumer: AIOKafkaConsumer | None = None
        # 自己的 PG/Qdrant 连接，不共享 MemoryWorker 的
        self.memory_repo = MemoryRepository()
        self.qdrant_repo = get_qdrant_repo()
        self.redis_repo = RedisRepository()
        self._memory_service: MemoryService | None = None

    async def start(self):
        await self.memory_repo.start()
        await self.qdrant_repo.start()
        # MemoryService 已经有 embedder（LM Studio），复用它
        self._memory_service = MemoryService(
            self.redis_repo,
            self.memory_repo,
            self.qdrant_repo,
        )

        self.consumer = AIOKafkaConsumer(
            settings.KAFKA_MEMORY_RECONCILE_TOPIC,
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            group_id=settings.KAFKA_MEMORY_RECONCILE_GROUP,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            max_poll_interval_ms=600_000,
            session_timeout_ms=60_000,
            heartbeat_interval_ms=20_000,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            key_deserializer=lambda k: k.decode("utf-8") if k else None,
        )
        try:
            await self.consumer.start()
            logger.info(
                "ReconcileConsumer started: topic=%s group=%s",
                settings.KAFKA_MEMORY_RECONCILE_TOPIC,
                settings.KAFKA_MEMORY_RECONCILE_GROUP,
            )
        except Exception as e:
            # Topic 不存在 / Kafka 不通 → 只 log warning，别让 lifespan 挂
            # aiokafka 在 topic 不存在时会抛错误（非阻塞），这里兜底
            logger.warning(
                "ReconcileConsumer.start() FAILED (will retry in consume loop). "
                "topic=%s reason=%s. "
                "Create the topic with: "
                "kafka-topics.bat --create --topic memory.reconcile --bootstrap-server localhost:9092",
                settings.KAFKA_MEMORY_RECONCILE_TOPIC, e,
            )
            self.consumer = None

    async def stop(self):
        if self.consumer:
            await self.consumer.stop()
            self.consumer = None

        await self.qdrant_repo.stop()
        await self.memory_repo.stop()
        # MemoryService.llm/embedder 是 AsyncOpenAI 客户端，不用显式 close
        self._memory_service = None

        logger.info("ReconcileConsumer stopped")

    async def consume(self):
        """
        主消费循环。

        如果 start() 时 topic 不存在 / Kafka 不通 → consumer=None，
        这里会每 30s 重试一次 start()，不会无限报错空转。
        """
        while True:
            if not self.consumer:
                logger.info("[Reconcile] consumer not ready, retry start() in 30s...")
                await asyncio.sleep(30)
                try:
                    # 重新初始化 consumer 对象再 start
                    self.consumer = AIOKafkaConsumer(
                        settings.KAFKA_MEMORY_RECONCILE_TOPIC,
                        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
                        group_id=settings.KAFKA_MEMORY_RECONCILE_GROUP,
                        auto_offset_reset="earliest",
                        enable_auto_commit=False,
                        max_poll_interval_ms=600_000,
                        session_timeout_ms=60_000,
                        heartbeat_interval_ms=20_000,
                        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                        key_deserializer=lambda k: k.decode("utf-8") if k else None,
                    )
                    await self.consumer.start()
                    logger.info("[Reconcile] consumer recovered ✅")
                except Exception as e:
                    logger.warning("[Reconcile] retry start() still failed: %s", e)
                    self.consumer = None
                continue

            try:
                async for message in self.consumer:
                    try:
                        payload = message.value or {}
                        batch_size = int(
                            payload.get("batch_size", settings.RECONCILE_BATCH_SIZE)
                        )

                        logger.info(
                            "[Reconcile] trigger: partition=%s offset=%s "
                            "batch_size=%d",
                            message.partition, message.offset, batch_size,
                        )

                        await self.reconcile_once(batch_size)

                        await self._safe_commit(message.offset)

                    except Exception:
                        logger.exception("[Reconcile] processing failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[Reconcile] consumer loop crashed, will retry start(): %s", e)
                self.consumer = None
                try:
                    await self.consumer.stop()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 对账核心逻辑
    # ------------------------------------------------------------------

    async def reconcile_once(self, batch_size: int | None = None):
        """
        一轮对账：扫 pending → 逐条重跑 embed+Qdrant → 记状态。
        """
        batch_size = batch_size or settings.RECONCILE_BATCH_SIZE
        total_pending = await self.memory_repo.count_pending()
        logger.info(
            "[Reconcile] START: total_pending=%d, batch_size=%d",
            total_pending, batch_size,
        )

        if total_pending == 0:
            logger.info("[Reconcile] nothing to do, exit")
            return

        items = await self.memory_repo.list_pending(limit=batch_size)
        if not items:
            logger.info("[Reconcile] no items ready (all in backoff), exit")
            return

        logger.info("[Reconcile] picked %d items to process", len(items))

        success = 0
        failed = 0

        for idx, item in enumerate(items):
            mid = item["memory_id"]
            logger.info(
                "[Reconcile] [%d/%d] mid=%s retries=%d type=%s content=%.50s",
                idx + 1, len(items), mid,
                item["sync_retry_count"],
                item["memory_type"], item["content"],
            )

            try:
                ok = await self._reindex_one(item)
                if ok:
                    success += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("[Reconcile] unexpected error for mid=%s", mid)
                failed += 1
                # 兜底 mark_pending_failed，累加 retry_count
                try:
                    await self.memory_repo.mark_pending_failed(
                        mid, "reconcile unexpected error"
                    )
                except Exception:
                    logger.exception(
                        "[Reconcile] fallback mark_pending_failed also failed mid=%s",
                        mid,
                    )

            # 批次间 sleep，控 embedding/Qdrant 速率
            if idx < len(items) - 1:
                await asyncio.sleep(settings.RECONCILE_ITEM_INTERVAL)

        logger.info(
            "[Reconcile] DONE: success=%d failed=%d total_scanned=%d",
            success, failed, len(items),
        )

    async def _reindex_one(self, item: dict) -> bool:
        """
        重跑单条孤儿记忆的 embed + Qdrant。

        同时检查近似合并：如果孤儿记忆和同类型已落库记忆 cosine ≥ 阈值，
        就把孤儿那条标 merged_into 指向保留的那条，不再补 embed/Qdrant。

        Returns:
            True  = 已修复（mark_synced 或 mark_merged）
            False = 仍 pending（mark_pending_failed，累加 backoff）
        """
        if self._memory_service is None:
            logger.error("ReconcileConsumer: MemoryService not initialized")
            return False

        vec = await self._memory_service.embed(item["content"])
        if not vec:
            await self.memory_repo.mark_pending_failed(
                item["memory_id"],
                "Embedding returned None during reconcile (LM Studio unreachable?)",
            )
            return False

        # 近似合并：孤儿记忆也可能和有效记忆近似 → 直接标 merged_into，不补 Qdrant
        try:
            similar = await self.qdrant_repo.find_similar_in_type(
                user_id=item["user_id"],
                memory_type=item["memory_type"],
                vector=vec,
                threshold=settings.MEMORY_MERGE_VECTOR_THRESHOLD,
            )
            if similar and similar[0]["memory_id"] != str(item["memory_id"]):
                # 命中且不是自己自己（孤儿自己还没 Qdrant，所以 find_similar 不会返回自己）
                keep_id = similar[0]["memory_id"]
                logger.info(
                    "[Reconcile] MERGE orphan=%s → keep=%s similarity=%.4f",
                    item["memory_id"], keep_id, similar[0]["score"],
                )
                await self.memory_repo.mark_merged(str(item["memory_id"]), keep_id)
                return True
        except Exception:
            logger.exception(
                "[Reconcile] find_similar_in_type FAILED (skip merge check) memory_id=%s",
                item["memory_id"],
            )

        try:
            await self.qdrant_repo.upsert(
                item["memory_id"],
                vec,
                user_id=item["user_id"],
                memory_type=item["memory_type"],
                importance=item["importance"],
                is_active=True,
            )
        except Exception as e:
            await self.memory_repo.mark_pending_failed(
                item["memory_id"],
                f"Qdrant upsert failed during reconcile: {type(e).__name__}: {e}",
            )
            return False

        # 成功
        await self.memory_repo.mark_synced(item["memory_id"])
        return True

    # ------------------------------------------------------------------
    # Kafka commit（复用 MemoryWorker 的模式）
    # ------------------------------------------------------------------

    async def _safe_commit(self, offset: int):
        if not self.consumer:
            return
        try:
            await self.consumer.commit()
        except CommitFailedError:
            logger.warning(
                "[Reconcile] commit offset=%s failed (rebalance), will retry after rejoin.",
                offset,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[Reconcile] commit offset=%s failed with unexpected error",
                offset,
            )


reconcile_consumer = ReconcileConsumer()
