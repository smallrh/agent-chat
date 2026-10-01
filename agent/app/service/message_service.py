import logging
import uuid

from app.core.config import settings
from app.kafka.events import MemoryEvent
from app.repo.outbox_repo import outbox_repo


logger = logging.getLogger(__name__)


class MessageService:
    """
    消息写入服务。

    改造后采用 Outbox Pattern：
      1. Chat API 调用 → 构造 MemoryEvent → 落 outbox_events 表
      2. OutboxRelay（后台 asyncio task）扫 outbox → 发 Kafka
      3. KafkaConsumer 照常消费（message_repo.save_message → redis_repo.push_message → memory_task）

    好处：Kafka 宕机时 Chat API 照常返回，outbox 留痕，relay 恢复后自动补发。
    """

    def __init__(self):
        self._repo_started = False

    async def _ensure_repo(self):
        """懒启动 outbox_repo（MessageService 第一次被调用时触发）。"""
        if not self._repo_started:
            await outbox_repo.start()
            self._repo_started = True

    # --------------------------------------------------------------
    # 公共写入接口
    # --------------------------------------------------------------

    async def create_user_message(
        self,
        user_id: int,
        conversation_id: str,
        content: str,
    ) -> str:
        """落一条 user 消息到 outbox。返回 message_id。"""

        await self._ensure_repo()

        message_id = str(uuid.uuid4())

        event = MemoryEvent(
            event_id=str(uuid.uuid4()),
            event_type="message.created",
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            role="user",
            content=content,
        )

        await self._outbox_insert(event)

        return message_id

    async def create_assistant_message(
        self,
        user_id: int,
        conversation_id: str,
        content: str,
    ) -> str:
        """落一条 assistant 消息到 outbox。返回 message_id。"""

        await self._ensure_repo()

        message_id = str(uuid.uuid4())

        event = MemoryEvent(
            event_id=str(uuid.uuid4()),
            event_type="message.created",
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            role="assistant",
            content=content,
        )

        await self._outbox_insert(event)

        return message_id

    # --------------------------------------------------------------
    # 内部实现
    # --------------------------------------------------------------

    async def _outbox_insert(self, event: MemoryEvent) -> None:
        """往 outbox_events 插一条记录。纯 PG 操作，不依赖 Kafka。"""

        try:
            await outbox_repo.insert_event(
                event_id=event.event_id,
                topic=settings.KAFKA_MEMORY_TOPIC,
                key=event.conversation_id,
                payload=event.model_dump(mode="json"),
            )
            logger.debug(
                "MessageService._outbox_insert OK: "
                "event_id=%s conv=%s role=%s",
                event.event_id,
                event.conversation_id,
                event.role,
            )
        except Exception:
            # outbox 写入失败是 PG 故障，直接向上抛
            # ChatService 的 stream_chat 里 catch 了会打 log，不阻断 SSE
            logger.exception(
                "MessageService._outbox_insert FAILED: "
                "event_id=%s conv=%s",
                event.event_id,
                event.conversation_id,
            )
            raise


message_service = MessageService()
