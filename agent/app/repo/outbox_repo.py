import json
import logging
import uuid as _uuid
from typing import Any

import asyncpg

from app.core.config import settings
from app.repo.pg import get_pool

logger = logging.getLogger(__name__)


# outbox_events.status 常量
STATUS_PENDING = 0             # 待 relay 发 Kafka
STATUS_SENT = 1                # 已发 Kafka
STATUS_FAILED_EXHAUSTED = 2    # 重试耗尽，已直写 PG 兜底（终态）
STATUS_PROCESSING = 3          # 被某个 relay claim 中，正在发 Kafka


class OutboxRepository:
    """
    outbox_events 表操作。

    所有操作都是轻量 SQL，不在事务里（MessageService 先落 outbox 再返回）；
    真正的 Kafka 发送由 OutboxRelay 后台 task 消费。
    """

    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    # --------------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------------

    async def start(self):
        """取共享连接池（幂等，已有池直接复用）。见 app/repo/pg.py。"""
        if self.pool is not None:
            return
        self.pool = await get_pool()
        logger.info("OutboxRepository started (shared PostgreSQL pool)")

    async def stop(self):
        # 共享池不在这里关闭（别的组件还在用），只解引用。
        self.pool = None

    # --------------------------------------------------------------
    # 写入（Chat API 路径调用）
    # --------------------------------------------------------------

    async def insert_event(
        self,
        event_id: str,
        topic: str,
        key: str | None,
        payload: dict[str, Any],
    ) -> None:
        """落一条 outbox 记录。event_id UNIQUE 保证幂等。"""

        if not self.pool:
            raise RuntimeError("OutboxRepository pool is not started")

        try:
            await self.pool.execute(
                """
                INSERT INTO outbox_events (
                    event_id, topic, key, payload, status, created_at
                )
                VALUES ($1, $2, $3, $4::jsonb, $5, now())
                ON CONFLICT (event_id) DO NOTHING
                """,
                _uuid.UUID(event_id),
                topic,
                key,
                json.dumps(payload, ensure_ascii=False),
                STATUS_PENDING,
            )
        except Exception:
            logger.exception(
                "OutboxRepository.insert_event FAILED: event_id=%s",
                event_id,
            )
            raise

    # --------------------------------------------------------------
    # Relay 消费
    # --------------------------------------------------------------

    async def claim_pending(
        self,
        batch_size: int = 50,
    ) -> list[asyncpg.Record]:
        """
        抢一批 status=PENDING 的待发送记录。

        用 UPDATE ... SET status=PROCESSING RETURNING ... 原子操作：
        - claim 后行状态变成 PROCESSING，其他 relay 扫不到
        - 多 relay 并发安全（PG 行锁保证 UPDATE 不冲突）
        """

        if not self.pool:
            raise RuntimeError("OutboxRepository pool is not started")

        rows = await self.pool.fetch(
            """
            UPDATE outbox_events
            SET status = $1
            WHERE id IN (
                SELECT id FROM outbox_events
                WHERE status = $2
                ORDER BY id ASC
                LIMIT $3
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, event_id, topic, key, payload
            """,
            STATUS_PROCESSING,   # $1
            STATUS_PENDING,      # $2
            batch_size,          # $3
        )
        return rows

    async def mark_sent(self, ids: list[int]) -> None:
        """标记成功发送：PROCESSING → SENT。"""

        if not ids:
            return

        if not self.pool:
            raise RuntimeError("OutboxRepository pool is not started")

        await self.pool.execute(
            """
            UPDATE outbox_events
            SET status = $1,
                sent_at = now()
            WHERE id = ANY($2)
              AND status = $3
            """,
            STATUS_SENT,          # $1
            ids,                   # $2
            STATUS_PROCESSING,     # $3
        )

    async def mark_failed(
        self,
        outbox_id: int,
        error: str,
    ) -> None:
        """标记一次失败：PROCESSING → PENDING（retry_count++，下次可重试）。"""

        if not self.pool:
            raise RuntimeError("OutboxRepository pool is not started")

        await self.pool.execute(
            """
            UPDATE outbox_events
            SET status = $1,
                retry_count = retry_count + 1,
                error = $2
            WHERE id = $3
              AND status = $4
            """,
            STATUS_PENDING,          # $1
            error[:500],             # $2
            outbox_id,               # $3
            STATUS_PROCESSING,       # $4
        )

    async def mark_failed_exhausted(
        self,
        outbox_id: int,
        error: str,
    ) -> None:
        """标记重试耗尽：PROCESSING → FAILED_EXHAUSTED（终态，不再重试）。"""

        if not self.pool:
            raise RuntimeError("OutboxRepository pool is not started")

        await self.pool.execute(
            """
            UPDATE outbox_events
            SET status = $1,
                retry_count = retry_count + 1,
                error = $2,
                sent_at = now()
            WHERE id = $3
              AND status = $4
            """,
            STATUS_FAILED_EXHAUSTED,  # $1
            error[:500],              # $2
            outbox_id,                # $3
            STATUS_PROCESSING,        # $4
        )

    # --------------------------------------------------------------
    # 自愈 & 清理
    # --------------------------------------------------------------

    async def recover_stale_processing(
        self,
        timeout_minutes: int = 5,
    ) -> int:
        """
        把 PROCESSING 超过 timeout_minutes 的行重置成 PENDING。

        处理场景：relay claim 了行后崩溃，行永远卡在 PROCESSING。
        调用时机：OutboxRelay 启动时 + 定期自检。

        返回被重置的行数。
        """

        if not self.pool:
            raise RuntimeError("OutboxRepository pool is not started")

        result = await self.pool.execute(
            """
            UPDATE outbox_events
            SET status = $1
            WHERE status = $2
              AND created_at < now() - make_interval(mins => $3)
            """,
            STATUS_PENDING,           # $1
            STATUS_PROCESSING,        # $2
            timeout_minutes,          # $3 — int，PG make_interval 构造
        )
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.warning(
                "OutboxRepository.recover_stale_processing: "
                "reset %d rows (timeout=%dmin)",
                count, timeout_minutes,
            )
        return count

    async def cleanup_expired(
        self,
        retention_days: int = 7,
    ) -> int:
        """
        删除终态（SENT / FAILED_EXHAUSTED）且超过 retention_days 天的记录。

        优先按 sent_at 删，没有 sent_at 的按 created_at。
        返回删除行数。
        """

        if not self.pool:
            raise RuntimeError("OutboxRepository pool is not started")

        result = await self.pool.execute(
            """
            DELETE FROM outbox_events
            WHERE status IN ($1, $2)
              AND COALESCE(sent_at, created_at) < now() - make_interval(days => $3)
            """,
            STATUS_SENT,                  # $1
            STATUS_FAILED_EXHAUSTED,      # $2
            retention_days,               # $3 — int，PG make_interval 构造
        )

        # asyncpg execute 返回 "DELETE N"
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(
                "OutboxRepository.cleanup_expired: deleted %d rows "
                "(retention_days=%d)",
                count, retention_days,
            )
        return count


outbox_repo = OutboxRepository()
