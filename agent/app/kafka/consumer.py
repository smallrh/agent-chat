import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from typing import Any

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import CommitFailedError

from app.core.config import settings
from app.kafka.producer import kafka_producer
from app.repo.message_repo import MessageRepository
from app.repo.redis_repo import RedisRepository


logger = logging.getLogger(__name__)


class NonRetryableError(Exception):
    """不可重试异常：脏数据 / schema 漂移 / 明显的编程错误。

    consumer 收到后直接进 DLQ，不做 retry。
    """


# ---------------------------------------------------------------------------
# 异常分类：决定 retry 还是直接 DLQ
# ---------------------------------------------------------------------------

def _is_non_retryable(exc: Exception) -> bool:
    """True = 不可重试，直接 DLQ。"""

    # 显式标记
    if isinstance(exc, NonRetryableError):
        return True

    # JSON 解析失败 → 脏数据
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return True

    # dict 结构错误 → payload 缺字段或类型错
    if isinstance(exc, (KeyError, TypeError, AttributeError)):
        return True

    return False


class KafkaMemoryConsumer:

    def __init__(self):
        self.consumer: AIOKafkaConsumer | None = None
        self.message_repo = MessageRepository()
        self.redis_repo = RedisRepository()

    async def start(self):
        await self.message_repo.start()
        await self.redis_repo.start()

        self.consumer = AIOKafkaConsumer(
            settings.KAFKA_MEMORY_TOPIC,

            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            group_id=settings.KAFKA_CONSUMER_GROUP,

            auto_offset_reset="earliest",
            enable_auto_commit=False,

            # 加大 rebalance 相关超时，避免 reload 模式下多进程
            # 抢同一个 consumer group 时频繁 rebalance
            max_poll_interval_ms=600_000,
            session_timeout_ms=60_000,
            heartbeat_interval_ms=20_000,

            # ★ 关键：deserializer 永不 raise
            # aiokafka 把 deserializer 异常包在 async generator 内部，
            # 会导致 async for 直接断，我们外层 try/except 接不到。
            # 这里用 safe 版本：失败时返回带 __deserialize_error__ 标记的 dict，
            # 后面 _deserialize() 检测到就直接 NonRetryableError → DLQ。
            value_deserializer=self._safe_value_deserializer,
            key_deserializer=lambda key: (
                key.decode("utf-8")
                if key
                else None
            ),
        )

        try:
            await self.consumer.start()
            logger.info(
                "Kafka consumer started: topic=%s group=%s",
                settings.KAFKA_MEMORY_TOPIC,
                settings.KAFKA_CONSUMER_GROUP,
            )
        except Exception as e:
            # Kafka 临时抖动 / topic 不存在 → 只 warning，不炸 lifespan
            logger.warning(
                "KafkaMemoryConsumer.start() FAILED (will retry in consume loop). "
                "topic=%s reason=%s",
                settings.KAFKA_MEMORY_TOPIC, e,
            )
            self.consumer = None

    @staticmethod
    def _safe_value_deserializer(value: bytes | None) -> Any:
        """永不 raise 的 value deserializer。

        - 成功 → 返回 dict（正常 MemoryEvent）
        - 失败 → 返回 {"__deserialize_error__": "...", "__raw__": "..."} 标记
        - value=None（tombstone） → 返回 None
        """

        if value is None:
            return None

        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as e:
            return {
                "__deserialize_error__": f"utf-8 decode failed: {e}",
                "__raw__": repr(value[:200]),
            }

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            return {
                "__deserialize_error__": f"json parse failed: {e}",
                "__raw__": text[:500],
            }

    async def stop(self):
        if self.consumer:
            await self.consumer.stop()
            self.consumer = None

        await self.message_repo.stop()
        await self.redis_repo.stop()

        logger.info("Kafka consumer stopped")

    # ====================================================================
    # 主消费循环 —— 核心改动：每条消息独立 retry + DLQ + finally 里 commit
    # ====================================================================

    async def consume(self):
        """主消费循环。consumer=None 时每 30s 重试 start()。"""
        while True:
            if not self.consumer:
                logger.info("[KafkaMemoryConsumer] consumer not ready, retry in 30s...")
                await asyncio.sleep(30)
                try:
                    self.consumer = AIOKafkaConsumer(
                        settings.KAFKA_MEMORY_TOPIC,
                        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
                        group_id=settings.KAFKA_CONSUMER_GROUP,
                        auto_offset_reset="earliest",
                        enable_auto_commit=False,
                        max_poll_interval_ms=600_000,
                        session_timeout_ms=60_000,
                        heartbeat_interval_ms=20_000,
                        value_deserializer=self._safe_value_deserializer,
                        key_deserializer=lambda key: key.decode("utf-8") if key else None,
                    )
                    await self.consumer.start()
                    logger.info("[KafkaMemoryConsumer] recovered ✅")
                except Exception as e:
                    logger.warning("[KafkaMemoryConsumer] retry still failed: %s", e)
                    self.consumer = None
                continue

            try:
                async for message in self.consumer:
                    try:
                        # 1) 先反序列化（脏数据直接进 DLQ，不 retry）
                        try:
                            event = self._deserialize(message)
                        except NonRetryableError as e:
                            await self._send_to_dlq(message, error=str(e))
                            continue

                        # 2) 正常消费路径：带 retry 的 handle
                        await self._process_with_retry(event, message)

                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # retry 耗尽 / 意外异常 → DLQ，不阻塞
                        logger.exception(
                            "Kafka consume loop unexpected error, sending DLQ"
                        )
                        try:
                            await self._send_to_dlq(message, error=str(exc))
                        except Exception:
                            logger.exception(
                                "DLQ send failed for offset=%d — event lost!",
                                message.offset,
                            )
                    finally:
                        # 3) 无论成功 / retry 后成功 / DLQ / 任何情况，都推进 offset
                        await self._safe_commit(message.offset)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(
                    "[KafkaMemoryConsumer] consumer loop crashed, will retry: %s", e,
                )
                # 先把崩溃的实例取出来再置 None —— 顺序反了会对 None 调用
                # stop()，必然 AttributeError 被下面的 except 吞掉，
                # 旧 consumer 的心跳线程与 socket 就泄漏了。
                old = self.consumer
                self.consumer = None
                if old is not None:
                    try:
                        await old.stop()
                    except Exception:
                        logger.exception(
                            "[KafkaMemoryConsumer] stop broken consumer failed "
                            "(ignored)"
                        )

    # ====================================================================
    # 反序列化：识别脏数据
    # ====================================================================

    @staticmethod
    def _deserialize(message) -> dict:
        """从 Kafka message 安全提取 event dict。

        注：value_deserializer 用的是 safe 版本，永不 raise；
        如果它失败，会返回带 __deserialize_error__ 标记的 dict。
        这里检测该标记 → 抛 NonRetryableError 直接进 DLQ。
        """

        raw = message.value
        if raw is None:
            raise NonRetryableError("message.value is None (tombstone / null)")

        if isinstance(raw, dict):
            # safe deserializer 返回的脏数据标记
            if "__deserialize_error__" in raw:
                raise NonRetryableError(
                    f"value_deserializer failed: {raw['__deserialize_error__']}"
                )
            return raw

        # 兜底：理论上 safe deserializer 不会返回非 dict
        raise NonRetryableError(
            f"unexpected value type after safe deserializer: {type(raw).__name__}"
        )

    # ====================================================================
    # 带 retry 的处理逻辑（内部再分：可重试 vs 不可重试）
    # ====================================================================

    async def _process_with_retry(
        self,
        event: dict,
        message,
    ) -> None:
        """最多 CONSUMER_MAX_RETRIES 次，指数退避 + 随机抖动。

        - 不可重试异常 → 立即 DLQ（不等 retry 耗尽）
        - 可重试异常 → 退避重试，耗尽后 DLQ
        - 全程异常由调用方 consume() 的 except 兜住，
          最终 finally 里 commit offset
        """

        max_retries = settings.CONSUMER_MAX_RETRIES

        for attempt in range(max_retries):
            try:
                await self.handle_event(event)
                # 成功处理 → 正常返回（consume() 里会 commit）
                return

            except NonRetryableError as e:
                # 不可重试，立即 DLQ
                logger.error(
                    "NonRetryableError, DLQ immediately: "
                    "partition=%d offset=%d err=%s",
                    message.partition, message.offset, str(e)[:200],
                )
                await self._send_to_dlq(message, error=str(e))
                return  # 直接 return，不再 retry

            except Exception as e:
                # 可重试异常
                if attempt >= max_retries - 1:
                    # 最后一次失败 → DLQ
                    logger.error(
                        "Retries exhausted (%d/%d), sending DLQ: "
                        "partition=%d offset=%d err=%s",
                        attempt + 1, max_retries,
                        message.partition, message.offset, str(e)[:200],
                    )
                    await self._send_to_dlq(message, error=str(e))
                    return

                # 还能 retry → 算退避时间
                delay = min(
                    settings.CONSUMER_RETRY_BASE_DELAY * (2 ** attempt),
                    settings.CONSUMER_RETRY_MAX_DELAY,
                )
                delay += random.uniform(0, settings.CONSUMER_RETRY_JITTER)

                logger.warning(
                    "Retry %d/%d after %.2fs: partition=%d offset=%d err=%s",
                    attempt + 1, max_retries,
                    delay, message.partition, message.offset, str(e)[:200],
                )
                await asyncio.sleep(delay)

    # ====================================================================
    # DLQ 发送
    # ====================================================================

    async def _send_to_dlq(self, message, error: str) -> None:
        """把失败消息投到 DLQ topic，保留完整上下文。"""

        # event 兜底：deserialize 过了就是 dict，没过就存 raw bytes
        event_payload: Any = None
        try:
            event_payload = self._deserialize(message)
        except Exception:
            # deserialize 也炸（典型场景），把 raw 转成可 JSON 的形式
            raw = message.value
            if isinstance(raw, bytes):
                try:
                    event_payload = raw.decode("utf-8")
                except Exception:
                    event_payload = f"<binary {len(raw)} bytes>"
            else:
                event_payload = str(raw)

        dlq_payload = {
            "original_topic": message.topic,
            "original_partition": message.partition,
            "original_offset": message.offset,
            "original_key": message.key,
            "event": event_payload,
            "error": error[:2000],
            "retries": settings.CONSUMER_MAX_RETRIES,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        # 用 DLQ topic 发（producer 自带 serializer，直接传 dict）
        await kafka_producer.producer.send_and_wait(
            topic=settings.KAFKA_MEMORY_DLQ_TOPIC,
            key=(message.key.encode("utf-8") if message.key else None),
            value=dlq_payload,
        )

        logger.error(
            "DLQ sent ✅ topic=%s partition=%d offset=%d",
            settings.KAFKA_MEMORY_DLQ_TOPIC,
            message.partition,
            message.offset,
        )

    # ====================================================================
    # offset commit（安全包装）
    # ====================================================================

    async def _safe_commit(self, offset: int) -> None:
        """
        安全地提交 offset。rebalance 失败降级为 warning。

        注：调用方已经在 consume() 的 finally 里调，
        所以无论 handle_event 成功 / retry 耗尽 / DLQ 都推进 offset。
        """

        if not self.consumer:
            return

        try:
            await self.consumer.commit()
        except CommitFailedError:
            logger.warning(
                "Commit offset=%d failed (rebalance in progress)", offset,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Commit offset=%d failed", offset)

    # ====================================================================
    # 业务处理（handle_event / handle_message_created / publish_memory_task）
    # 保持不变，仅改 exception 语义：这里抛出的异常会被 _process_with_retry
    # 自动分类（可 retry vs 不可 retry）
    # ====================================================================

    async def handle_event(self, event: dict) -> None:
        event_type = event.get("event_type")
        if event_type == "message.created":
            await self.handle_message_created(event)
        else:
            # 未知 event_type → 直接 NonRetryableError 进 DLQ
            raise NonRetryableError(
                f"unknown event_type={event_type!r}"
            )

    async def handle_message_created(self, event: dict) -> None:
        """
        单条 message.created 消费。任何异常抛到上层，
        由 _process_with_retry 分类处理（retry 还是 DLQ）。
        """

        # payload 结构校验（缺字段 → NonRetryableError）
        for f in ("message_id", "conversation_id"):
            if not event.get(f):
                raise NonRetryableError(
                    f"message.created missing required field: {f}"
                )

        mid = event["message_id"]
        cid = event["conversation_id"]

        # --- Step 1: PG 落库（幂等 ON CONFLICT DO NOTHING）---
        sequence_id = await self.message_repo.save_message(event)
        event["sequence_id"] = sequence_id

        # --- Step 2: Redis push（会话上下文）---
        await self.redis_repo.push_message(event)

        # --- Step 3: Memory task 投递（独立 topic / 独立 consumer）---
        # 这里吞异常不 retry：
        # memory worker 本身就是异步独立 consumer，
        # 主链路（PG + Redis）已落库成功，memory task 失败不阻塞对话恢复。
        try:
            await self.publish_memory_task(event)
        except Exception:
            logger.exception(
                "publish_memory_task failed "
                "(non-fatal, won't retry): conv=%s mid=%s",
                cid, mid,
            )

        logger.info(
            "message.created handled ✅ conv=%s mid=%s seq=%d",
            cid, mid, sequence_id,
        )

    async def publish_memory_task(self, event: dict) -> None:
        """投递 memory 处理任务到独立 topic。"""
        task = {
            "task_type": "memory.process",
            "user_id": event["user_id"],
            "conversation_id": event["conversation_id"],
            "message_id": event["message_id"],
        }

        await kafka_producer.producer.send_and_wait(
            topic=settings.KAFKA_MEMORY_TASK_TOPIC,
            key=event["conversation_id"].encode("utf-8"),
            value=task,   # dict，producer serializer 处理
        )


kafka_consumer = KafkaMemoryConsumer()
