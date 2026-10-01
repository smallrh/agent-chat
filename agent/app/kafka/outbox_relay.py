import asyncio
import json
import logging
from typing import Any

from app.repo.message_repo import MessageRepository
from app.repo.outbox_repo import outbox_repo
from app.kafka.producer import kafka_producer


logger = logging.getLogger(__name__)


# ---------- Relay 配置 ----------
RELAY_POLL_INTERVAL = 1.0          # 轮询间隔（秒）
RELAY_BATCH_SIZE = 50              # 每轮 claim 的批量
RELAY_MAX_RETRIES = 5              # 失败几次后走直写 PG 兜底（用户确认）
RELAY_RECOVER_INTERVAL = 60.0      # 自愈检查间隔（秒）：PROCESSING 超时重置
RELAY_RECOVER_TIMEOUT = 5          # PROCESSING 多少分钟算超时
RELAY_CLEANUP_INTERVAL = 3600.0    # 清理间隔（秒）：默认每小时
RELAY_CLEANUP_RETENTION_DAYS = 7   # 终态保留天数（用户确认）


class OutboxRelay:
    """
    Outbox Pattern 的 relay 进程。

    职责：
      1. 扫 outbox_events 表的 PENDING 行 → 发 Kafka
      2. 发成功 → mark_sent
      3. 发失败 → mark_failed（retry_count++，回到 PENDING）
      4. retry_count >= MAX_RETRIES → 直写 PG 兜底 → mark_failed_exhausted
      5. 定期清理终态、重置卡死的 PROCESSING 行

    作为 FastAPI lifespan 内的 asyncio task 运行。
    """

    def __init__(self):
        self._message_repo = MessageRepository()
        self._msg_repo_started = False
        self._running = True

    async def start(self):
        """启动前确保依赖就绪。"""
        await outbox_repo.start()
        await self._message_repo.start()
        self._msg_repo_started = True

        # 启动时先 reset 上次 crash 残留的 PROCESSING
        await outbox_repo.recover_stale_processing(
            timeout_minutes=RELAY_RECOVER_TIMEOUT,
        )

        logger.info(
            "OutboxRelay started "
            "(poll=%.1fs, batch=%d, max_retries=%d, cleanup=%dd/%ds)",
            RELAY_POLL_INTERVAL,
            RELAY_BATCH_SIZE,
            RELAY_MAX_RETRIES,
            RELAY_CLEANUP_RETENTION_DAYS,
            int(RELAY_CLEANUP_INTERVAL),
        )

    async def run(self):
        """主循环：claim → 发 Kafka → 标记结果。"""

        logger.info("OutboxRelay.run() main loop START")

        last_recover = asyncio.get_event_loop().time()
        last_cleanup = asyncio.get_event_loop().time()

        while self._running:
            try:
                now = asyncio.get_event_loop().time()

                # --- 自愈 & 清理（低频）---
                if now - last_recover > RELAY_RECOVER_INTERVAL:
                    await outbox_repo.recover_stale_processing(
                        timeout_minutes=RELAY_RECOVER_TIMEOUT,
                    )
                    last_recover = now

                if now - last_cleanup > RELAY_CLEANUP_INTERVAL:
                    await outbox_repo.cleanup_expired(
                        retention_days=RELAY_CLEANUP_RETENTION_DAYS,
                    )
                    last_cleanup = now

                # --- 主循环：claim + 发 Kafka ---
                rows = await outbox_repo.claim_pending(
                    batch_size=RELAY_BATCH_SIZE,
                )

                if not rows:
                    # 没有活，等下一轮
                    await asyncio.sleep(RELAY_POLL_INTERVAL)
                    continue

                # 逐条发送（同 conversation_id 进入同一 partition 天然有序）
                sent_ids: list[int] = []

                for row in rows:
                    ok = await self._send_one(row)
                    if ok:
                        sent_ids.append(row["id"])

                # 批量标记成功
                if sent_ids:
                    await outbox_repo.mark_sent(sent_ids)

            except asyncio.CancelledError:
                logger.info("OutboxRelay.run() CancelledError, exiting")
                break
            except Exception:
                # 单轮异常不退出主循环
                logger.exception("OutboxRelay.run() unexpected error, will retry")
                await asyncio.sleep(RELAY_POLL_INTERVAL)

        logger.info("OutboxRelay.run() main loop EXIT")

    async def stop(self):
        self._running = False
        logger.info("OutboxRelay.stop() called")

    # --------------------------------------------------------------
    # 内部：单条发送
    # --------------------------------------------------------------

    async def _send_one(self, row) -> bool:
        """
        发一条 outbox 记录到 Kafka。

        返回 True = 发成功，主循环会 mark_sent；
        返回 False = 发失败，已自行 mark_failed / mark_failed_exhausted。

        注意：kafka_producer.producer 自带 value_serializer
        (json.dumps + encode)，所以这里直接传 dict 就行，
        不要手动 json.dumps/encode 否则会 double-encode。
        """

        outbox_id = row["id"]
        event_id = str(row["event_id"])
        topic = row["topic"]
        key = row["key"]

        # PG jsonb → asyncpg 返回 dict，但保险起见兜底 str/bytes
        payload = self._ensure_dict(row["payload"])

        try:
            # 直接传 dict，producer 的 serializer 会处理 json.dumps + encode
            await kafka_producer.producer.send_and_wait(
                topic=topic,
                key=key.encode("utf-8") if key else None,
                value=payload,  # dict，不要手动 encode！
            )
            logger.debug(
                "OutboxRelay._send_one OK: outbox_id=%d event_id=%s",
                outbox_id, event_id,
            )
            return True

        except Exception as exc:
            logger.warning(
                "OutboxRelay._send_one FAILED: outbox_id=%d event_id=%s err=%s",
                outbox_id, event_id, str(exc)[:200],
            )

            await self._handle_failure(outbox_id, event_id, payload, str(exc))
            return False

    @staticmethod
    def _ensure_dict(payload: Any) -> dict:
        """PG jsonb → asyncpg 通常返回 dict，但兜底 bytes/str 类型。"""
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            return json.loads(payload)
        raise TypeError(f"Unexpected payload type: {type(payload)}")

    async def _handle_failure(
        self,
        outbox_id: int,
        event_id: str,
        payload: dict[str, Any],
        error: str,
    ) -> None:
        """发送失败处理：重试 or 直写 PG 兜底。"""

        # 查当前 retry_count
        row = await outbox_repo.pool.fetchrow(
            "SELECT retry_count FROM outbox_events WHERE id = $1",
            outbox_id,
        )
        current_retries = int(row["retry_count"]) if row else 0

        if current_retries >= RELAY_MAX_RETRIES:
            # 重试耗尽 → 直写 PG 兜底
            logger.error(
                "OutboxRelay._handle_failure: outbox_id=%d retries=%d, "
                "fallback to direct PG write for event_id=%s",
                outbox_id, current_retries, event_id,
            )
            try:
                # payload 就是 MemoryEvent 的 JSON，直接喂 MessageRepository.save_message
                await self._message_repo.save_message(payload)
                await outbox_repo.mark_failed_exhausted(outbox_id, error)
                logger.info(
                    "OutboxRelay._handle_failure: direct PG fallback OK "
                    "for outbox_id=%d event_id=%s",
                    outbox_id, event_id,
                )
            except Exception:
                logger.exception(
                    "OutboxRelay._handle_failure: even direct PG fallback "
                    "FAILED for outbox_id=%d event_id=%s — event lost!",
                    outbox_id, event_id,
                )
                # PG 也写失败，只能继续 mark_failed_exhausted 留痕
                await outbox_repo.mark_failed_exhausted(
                    outbox_id,
                    f"PG fallback also failed: {error}",
                )
        else:
            # 还有机会 → mark_failed 回到 PENDING
            await outbox_repo.mark_failed(outbox_id, error)


outbox_relay = OutboxRelay()
