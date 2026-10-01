import asyncio
import json
import logging

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import CommitFailedError

from app.core.config import settings
from app.kafka.memory_worker import MemoryWorker

logger = logging.getLogger(__name__)


class MemoryWorkerConsumer:
    """
    消费 memory.tasks topic，驱动 MemoryWorker 做异步记忆处理。

    与 KafkaMemoryConsumer 是两个独立的 consumer：
      - KafkaMemoryConsumer  → memory.events topic   → 落 PG + Redis + 投递 task
      - MemoryWorkerConsumer → memory.tasks topic    → Summary / Memory / Profile
    """

    def __init__(self):
        self.consumer: AIOKafkaConsumer | None = None
        self.worker = MemoryWorker()

    async def start(self):
        await self.worker.start()

        self.consumer = AIOKafkaConsumer(
            settings.KAFKA_MEMORY_TASK_TOPIC,
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            group_id=settings.KAFKA_MEMORY_WORKER_GROUP,
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
                "MemoryWorkerConsumer started: topic=%s group=%s",
                settings.KAFKA_MEMORY_TASK_TOPIC,
                settings.KAFKA_MEMORY_WORKER_GROUP,
            )
        except Exception as e:
            logger.warning(
                "MemoryWorkerConsumer.start() FAILED (will retry in consume loop). "
                "topic=%s reason=%s",
                settings.KAFKA_MEMORY_TASK_TOPIC, e,
            )
            self.consumer = None

    async def stop(self):
        if self.consumer:
            await self.consumer.stop()
            self.consumer = None

        await self.worker.stop()

        logger.info("MemoryWorkerConsumer stopped")

    async def consume(self):
        """主消费循环。consumer=None 时每 30s 重试 start()。"""
        while True:
            if not self.consumer:
                logger.info("[MemoryWorkerConsumer] consumer not ready, retry in 30s...")
                await asyncio.sleep(30)
                try:
                    self.consumer = AIOKafkaConsumer(
                        settings.KAFKA_MEMORY_TASK_TOPIC,
                        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
                        group_id=settings.KAFKA_MEMORY_WORKER_GROUP,
                        auto_offset_reset="earliest",
                        enable_auto_commit=False,
                        max_poll_interval_ms=600_000,
                        session_timeout_ms=60_000,
                        heartbeat_interval_ms=20_000,
                        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                        key_deserializer=lambda k: k.decode("utf-8") if k else None,
                    )
                    await self.consumer.start()
                    logger.info("[MemoryWorkerConsumer] recovered ✅")
                except Exception as e:
                    logger.warning("[MemoryWorkerConsumer] retry still failed: %s", e)
                    self.consumer = None
                continue

            try:
                async for message in self.consumer:
                    try:
                        task = message.value
                        logger.info(
                            "Memory task received: partition=%s offset=%s "
                            "task_type=%s conversation_id=%s",
                            message.partition, message.offset,
                            task.get("task_type"), task.get("conversation_id"),
                        )
                        await self.worker.handle_task(task)
                        await self._safe_commit(message.offset)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Memory task processing failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(
                    "[MemoryWorkerConsumer] consumer loop crashed, will retry: %s", e,
                )
                self.consumer = None
                try:
                    await self.consumer.stop()
                except Exception:
                    pass

    async def _safe_commit(self, offset: int):
        if not self.consumer:
            return
        try:
            await self.consumer.commit()
        except CommitFailedError:
            logger.warning(
                "Memory worker commit offset=%s failed (rebalance), "
                "will retry after rejoin.",
                offset,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Memory worker commit offset=%s failed with unexpected error",
                offset,
            )


memory_worker_consumer = MemoryWorkerConsumer()
