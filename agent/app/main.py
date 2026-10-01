import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.api.memory import router as memory_router
from app.core.config import settings
from app.kafka.consumer import kafka_consumer
from app.repo.qdrant_repo import get_qdrant_repo
from app.kafka.memory_worker_consumer import memory_worker_consumer
from app.kafka.outbox_relay import outbox_relay
from app.kafka.producer import kafka_producer
from app.kafka.reconcile_consumer import reconcile_consumer


# ====== 强制配置 Python logging ======
# uvicorn 不会自动给 app 里的 logger 加 handler，
# 必须显式配置否则 logger.info() / logger.warning() 会被静默丢弃
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    force=True,
)

# =========================
# 自己的项目
# =========================

logging.getLogger("app").setLevel(logging.DEBUG)


# =========================
# 第三方库
# =========================

logging.getLogger("aiokafka").setLevel(logging.WARNING)
logging.getLogger("kafka").setLevel(logging.WARNING)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logging.getLogger("asyncio").setLevel(logging.WARNING)

logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.error").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.INFO)

logging.getLogger("watchfiles").setLevel(logging.WARNING)

logging.getLogger("redis").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期。任何一步失败都不能让整个服务挂。
    """
    consumer_task: asyncio.Task | None = None
    worker_task: asyncio.Task | None = None
    relay_task: asyncio.Task | None = None
    reconcile_task: asyncio.Task | None = None
    reconcile_timer_task: asyncio.Task | None = None
    qdrant_health_task: asyncio.Task | None = None

    # ---- 1. Producer ----
    logger.warning(">>> [lifespan] STEP 1/4 starting kafka_producer...")
    try:
        await kafka_producer.start()
        logger.warning(">>> [lifespan] STEP 1/4 kafka_producer OK ✅")
    except Exception:
        logger.exception(">>> [lifespan] STEP 1/4 kafka_producer FAILED ❌")
        raise

    # ---- 2. Message Consumer ----
    logger.warning(">>> [lifespan] STEP 2/4 starting kafka_consumer...")
    try:
        await kafka_consumer.start()
        consumer_task = asyncio.create_task(
            kafka_consumer.consume(),
            name="message-consumer",
        )
        logger.warning(">>> [lifespan] STEP 2/4 kafka_consumer OK ✅")
    except Exception:
        logger.exception(">>> [lifespan] STEP 2/4 kafka_consumer FAILED ❌")
        raise

    # ---- 3. Memory Worker Consumer（隔离异常，失败不阻塞主服务）----
    logger.warning(">>> [lifespan] STEP 3/4 starting memory_worker_consumer...")
    try:
        await memory_worker_consumer.start()
        worker_task = asyncio.create_task(
            memory_worker_consumer.consume(),
            name="memory-worker",
        )
        logger.warning(">>> [lifespan] STEP 3/4 memory_worker_consumer OK ✅")
    except Exception:
        logger.exception(
            ">>> [lifespan] STEP 3/4 memory_worker_consumer FAILED ❌ "
            "(Chat API + Message Consumer 仍可用)"
        )
        worker_task = None

    # ---- 4. Outbox Relay（隔离异常，失败不阻塞主服务）----
    logger.warning(">>> [lifespan] STEP 4/5 starting outbox_relay...")
    try:
        await outbox_relay.start()
        relay_task = asyncio.create_task(
            outbox_relay.run(),
            name="outbox-relay",
        )
        logger.warning(">>> [lifespan] STEP 4/5 outbox_relay OK ✅")
    except Exception:
        logger.exception(
            ">>> [lifespan] STEP 4/5 outbox_relay FAILED ❌ "
            "(Chat API 可写 outbox，需手动补转发)"
        )
        relay_task = None

    # ---- 5. Reconcile Consumer（隔离异常，失败不阻塞主服务）----
    logger.warning(">>> [lifespan] STEP 5/5 starting reconcile_consumer...")
    try:
        await reconcile_consumer.start()
        reconcile_task = asyncio.create_task(
            reconcile_consumer.consume(),
            name="memory-reconcile",
        )
        logger.warning(">>> [lifespan] STEP 5/5 reconcile_consumer OK ✅")

        # 启动后自动发一条 reconcile trigger，扫一次历史 pending 行
        try:
            await kafka_producer.send_reconcile_trigger()
            logger.info(">>> [lifespan] auto reconcile trigger sent ✅")
        except Exception:
            logger.exception(
                ">>> [lifespan] auto reconcile trigger FAILED "
                "(可手动触发: send_reconcile_trigger)"
            )

        # 周期性对账：除启动时的一次外，每隔 N 秒再发一次触发，
        # 让 LM Studio 抖动脉冲期错过的 pending 记忆能在下一周期自动补齐。
        reconcile_timer_task = asyncio.create_task(
            _periodic_reconcile_loop(),
            name="reconcile-timer",
        )
        logger.warning(
            ">>> [lifespan] reconcile periodic timer started (every %ds) ✅",
            settings.RECONCILE_INTERVAL_SECONDS,
        )
    except Exception:
        logger.exception(
            ">>> [lifespan] STEP 5/5 reconcile_consumer FAILED ❌ "
            "(正常写入链路不受影响，孤儿记忆需手动修复)"
        )
        reconcile_task = None

    # ---- Qdrant 后台探活/重连（独立于其它步骤，失败不影响主服务）----
    # 解决「启动期 Qdrant 没就绪 / 中途重启」导致连接永久卡在 None 的问题：
    # 健康循环每 QDRANT_HEALTH_INTERVAL_SECONDS 秒探活一次，连上即恢复。
    try:
        qdrant_health_task = asyncio.create_task(
            _qdrant_health_loop(),
            name="qdrant-health",
        )
        logger.warning(
            ">>> [lifespan] qdrant health loop started (every %ds) ✅",
            settings.QDRANT_HEALTH_INTERVAL_SECONDS,
        )
    except Exception:
        logger.exception(">>> [lifespan] qdrant health loop FAILED ❌")
        qdrant_health_task = None

    yield

    logger.warning(">>> [lifespan] shutdown sequence start...")
    for task in (qdrant_health_task, reconcile_timer_task, reconcile_task, relay_task, consumer_task, worker_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    try:
        await reconcile_consumer.stop()
    except Exception:
        logger.exception("reconcile_consumer.stop() error (ignored)")
    try:
        await outbox_relay.stop()
    except Exception:
        logger.exception("outbox_relay.stop() error (ignored)")
    try:
        await memory_worker_consumer.stop()
    except Exception:
        logger.exception("memory_worker_consumer.stop() error (ignored)")
    try:
        await kafka_consumer.stop()
    except Exception:
        logger.exception("kafka_consumer.stop() error (ignored)")
    try:
        await kafka_producer.stop()
    except Exception:
        logger.exception("kafka_producer.stop() error (ignored)")
    logger.warning(">>> [lifespan] shutdown complete")


async def _periodic_reconcile_loop():
    """
    周期性对账触发（后台任务）。

    每隔 RECONCILE_INTERVAL_SECONDS 调一次 kafka_producer.send_reconcile_trigger()，
    复用和「启动时自动触发」完全相同的 Kafka 路径，由 reconcile_consumer 消费。

    设计要点：
      - 空闲时 reconcile_once 扫到 pending=0 直接 return，几乎零开销；
      - 有 pending 时自动补齐 embed+Qdrant，单条失败走指数退避，下一周期再试；
      - 这样即使 LM Studio embedding 偶发 502（子进程抖动），也不会造成 pending 长期积压。
    """
    interval = settings.RECONCILE_INTERVAL_SECONDS
    logger.warning(">>> [reconcile-timer] started, interval=%ds", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await kafka_producer.send_reconcile_trigger()
            logger.info(">>> [reconcile-timer] periodic trigger sent ✅")
        except Exception:
            logger.exception(
                ">>> [reconcile-timer] periodic trigger FAILED "
                "(Kafka/producer 不可用，下个周期会重试)"
            )


async def _qdrant_health_loop():
    """
    Qdrant 后台探活 / 重连（后台任务）。

    每隔 QDRANT_HEALTH_INTERVAL_SECONDS 调一次 qdrant_repo.ensure_connected()：
      - 已连接但探测失败（Qdrant 中途重启）→ 丢弃旧 client，下轮重连；
      - 未连接 → 尝试连接；连上即恢复。

    设计要点：
      - 覆盖「启动期 Qdrant 未就绪」「Qdrant 运行中途重启」两类场景，
        即使 app 一直空闲（没有 upsert/search 触发重连）也能自动恢复，
        不再出现「启动告警一次后永久连不上」的假死。
      - 探活本身是轻量 get_collections，开销极小。
    """
    repo = get_qdrant_repo()
    interval = settings.QDRANT_HEALTH_INTERVAL_SECONDS
    logger.warning(">>> [qdrant-health] started, interval=%ds", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            ok = await repo.ensure_connected()
            if ok:
                logger.debug(">>> [qdrant-health] connected ✅")
            else:
                logger.info(">>> [qdrant-health] not connected yet, retry next cycle")
        except Exception:
            logger.exception(">>> [qdrant-health] unexpected error (ignored)")


app = FastAPI(
    title="Agent System",
    description="AI Agent Backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(chat_router)
app.include_router(memory_router)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.RELOAD,
    )
