import json
import logging

from aiokafka import AIOKafkaProducer

from app.core.config import settings
from app.kafka.events import MemoryEvent


logger = logging.getLogger(__name__)


class KafkaProducer:

    def __init__(self):
        self.producer: AIOKafkaProducer | None = None

    async def start(self):
        self.producer = AIOKafkaProducer(
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda value: json.dumps(
                value,
                ensure_ascii=False
            ).encode("utf-8"),
        )

        await self.producer.start()

        logger.info(
            "Kafka producer started: %s",
            settings.KAFKA_BOOTSTRAP_SERVERS
        )

    async def stop(self):
        if self.producer:
            await self.producer.stop()
            self.producer = None

        logger.info("Kafka producer stopped")

    async def send_memory_event(
        self,
        event: MemoryEvent
    ):
        if not self.producer:
            raise RuntimeError(
                "Kafka producer is not started"
            )

        await self.producer.send_and_wait(
            topic=settings.KAFKA_MEMORY_TOPIC,

            # 相同 conversation_id 尽量进入同一个 partition
            key=event.conversation_id.encode("utf-8"),

            value=event.model_dump(mode="json"),
        )

        logger.debug(
            "Kafka event sent: event_id=%s, conversation_id=%s",
            event.event_id,
            event.conversation_id,
        )

    async def send_reconcile_trigger(
        self,
        batch_size: int | None = None,
    ):
        """
        触发一次孤儿记忆对账。

        发一条空消息到 memory.reconcile topic，
        reconcile_consumer 收到后会扫 PG 里所有 sync_status='pending' 的行
        重跑 embed + Qdrant。
        """
        if not self.producer:
            raise RuntimeError("Kafka producer is not started")

        payload = {
            "trigger": "manual" if batch_size else "auto",
            "batch_size": batch_size or settings.RECONCILE_BATCH_SIZE,
        }
        await self.producer.send_and_wait(
            topic=settings.KAFKA_MEMORY_RECONCILE_TOPIC,
            key=b"reconcile",
            value=payload,
        )
        logger.info(
            "Kafka reconcile trigger sent: batch_size=%d topic=%s",
            payload["batch_size"],
            settings.KAFKA_MEMORY_RECONCILE_TOPIC,
        )


kafka_producer = KafkaProducer()