import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router
from app.api.memory import router as memory_router
from app.api.tasks import router as tasks_router
from app.api.attachments import router as attachments_router
from app.api.ops import router as ops_router
from app.core.obs import (
    M_HTTP_REQUESTS,
    TRACE_HEADER,
    TraceIdFilter,
    metrics,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
)
from app.service.attachment_service import attachment_service
from app.service.task_service import task_service
from app.service.usage_service import usage_service
from app.core.config import settings
from app.core.auth import close_pool
from app.repo.pg import close_pool as close_pg_pool
from app.service.chat_service import chat_service
from app.service.conversation_service import conversation_service
from app.service.retrieval_service import memory_retrieval_service
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
    format=(
        "%(asctime)s | %(levelname)-8s | %(name)s | "
        "trace=%(trace_id)s | %(message)s"
    ),
    force=True,
)

# 给 root logger 的每个 handler 装上 trace_id 注入器。
# trace_id 存在 contextvar 里：主线程由下面的 HTTP 中间件设置，
# 线程池/主事件循环里的工具调用由 ToolGuard 的 trace_scope 恢复。
for _handler in logging.getLogger().handlers:
    _handler.addFilter(TraceIdFilter())

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

    # ---- 6. Task Service（P2 多任务：后台任务 runner）----
    # 隔离异常，失败不阻塞主服务（聊天/记忆写入链路不受影响）
    logger.warning(">>> [lifespan] STEP 6 starting task_service...")
    try:
        await task_service.start()
        logger.warning(">>> [lifespan] STEP 6 task_service OK ✅")
    except Exception:
        logger.exception(
            ">>> [lifespan] STEP 6 task_service FAILED ❌ "
            "(Chat API 正常，仅后台任务不可用)"
        )

    # ---- 7. Attachment Service（P2 多模态：上传落盘 + 文本抽取）----
    # 隔离异常，失败不阻塞主服务（只是不能传附件，对话照常）
    logger.warning(">>> [lifespan] STEP 7 starting attachment_service...")
    try:
        await attachment_service.start()
        logger.warning(">>> [lifespan] STEP 7 attachment_service OK ✅")
    except Exception:
        logger.exception(
            ">>> [lifespan] STEP 7 attachment_service FAILED ❌ "
            "(Chat API 正常，仅附件功能不可用)"
        )

    # ---- 8. Usage Service（P3 计费埋点）----
    # 最轻的一步：只是建个 Redis client，失败不影响任何功能（埋点会静默跳过）
    logger.warning(">>> [lifespan] STEP 8 starting usage_service...")
    try:
        await usage_service.start()
        logger.warning(">>> [lifespan] STEP 8 usage_service OK ✅")
    except Exception:
        logger.exception(
            ">>> [lifespan] STEP 8 usage_service FAILED ❌ "
            "(Chat API 正常，仅用量计数不可用)"
        )

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
        await task_service.stop()
    except Exception:
        logger.exception("task_service.stop() error (ignored)")

    try:
        await attachment_service.stop()
    except Exception:
        logger.exception("attachment_service.stop() error (ignored)")

    try:
        await usage_service.aclose()
    except Exception:
        logger.exception("usage_service.aclose() error (ignored)")

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

    # ---- 业务侧连接：以前这里漏了 chat_service 的 Redis / 各 Repo 的 PG 池，
    #      RELOAD=true 每次改文件重建进程都会留下一批连接，PG 连接数一路涨。
    try:
        await chat_service.aclose()
    except Exception:
        logger.exception("chat_service.aclose() error (ignored)")

    try:
        await conversation_service.aclose()
    except Exception:
        logger.exception("conversation_service.aclose() error (ignored)")

    try:
        await memory_retrieval_service.aclose()
    except Exception:
        logger.exception("memory_retrieval_service.aclose() error (ignored)")

    # ---- 最后统一关闭进程级共享 PG 池（app/repo/pg.py）与 auth pool ----
    # 顺序很重要：必须等业务组件都不再拿连接了才关。
    try:
        await close_pg_pool()
    except Exception:
        logger.exception("PG shared pool close error (ignored)")

    try:
        await close_pool()
    except Exception:
        logger.exception("auth.close_pool() error (ignored)")

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
app.include_router(conversations_router)
app.include_router(memory_router)
app.include_router(tasks_router)
app.include_router(attachments_router)
app.include_router(ops_router)


@app.middleware("http")
async def _trace_middleware(request, call_next):
    """为每个请求分配/透传 trace_id，并在响应头回写。

    上游（Go 网关）传了 X-Trace-Id 就沿用，没有就新生成一个 ——
    这样一次请求从 Go → Chat API → Agent → 工具 → Kafka 全链路同号。
    """
    trace_id = request.headers.get(TRACE_HEADER) or new_trace_id()
    token = set_trace_id(trace_id)

    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        metrics.inc(M_HTTP_REQUESTS, 1, status="error",
                    path=request.url.path, method=request.method)
        raise
    finally:
        reset_trace_id(token)

    response.headers[TRACE_HEADER] = trace_id
    metrics.inc(
        M_HTTP_REQUESTS, 1,
        status=str(status_code),
        method=request.method,
        path=request.url.path,
    )
    return response


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.RELOAD,
    )
