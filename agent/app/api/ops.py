"""运维端点：健康检查 + 指标。

    GET /healthz   liveness  —— 进程活着就行，K8s/Docker 用它决定要不要重启
    GET /readyz    readiness —— 依赖是否就绪，决定是否把流量打进来
    GET /metrics   指标快照（JSON），含工具/LLM 埋点与业务积压量

## 为什么 liveness 不探活依赖

liveness 探活依赖会造成「依赖抖一下 → 容器被重启 → 更多请求失败」的连锁反应。
所以 liveness 只看进程本身，依赖状态一律放 readiness。

## metrics 为什么是 JSON 而不是 Prometheus 文本格式

当前阶段没有 Prometheus/Grafana，JSON 更适合人读和脚本消费。
接入监控系统时改 `MetricsRegistry.snapshot()` 的序列化即可，埋点不用动。

## 访问控制

`/metrics` 会暴露调用量、工具名等内部信息：
  - `METRICS_ENABLED=False` 时返回 404
  - 配了 `METRICS_TOKEN` 时要求请求头 `X-Metrics-Token` 匹配
生产环境建议在 Go 网关层直接禁止外部访问这两个路径。
"""

from __future__ import annotations

import logging
import time

import asyncio

from fastapi import APIRouter, Header, HTTPException, Response, status

from app.core.config import settings
from app.core.obs import metrics

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Ops"])


# ---------------------------------------------------------------------------
# 依赖探活
# ---------------------------------------------------------------------------

async def _check_pg() -> tuple[bool, str]:
    """探活 PG：执行 SELECT 1，2 秒超时。"""
    try:
        from app.repo.pg import get_pool

        pool = await asyncio.wait_for(get_pool(), timeout=2.0)
        row = await asyncio.wait_for(pool.fetchval("SELECT 1"), timeout=2.0)
        return (row == 1), "ok"
    except Exception as exc:                              # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


async def _check_redis() -> tuple[bool, str]:
    """探活 Redis：新建一条短连接 ping 一次（不占用业务连接）。"""
    try:
        import redis.asyncio as redis

        client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=getattr(settings, "REDIS_PASSWORD", None),
            db=getattr(settings, "REDIS_DB", 0),
            socket_connect_timeout=2.0,
        )
        try:
            await asyncio.wait_for(client.ping(), timeout=2.0)
            return True, "ok"
        finally:
            await client.aclose()
    except Exception as exc:                              # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


async def _check_qdrant() -> tuple[bool, str]:
    """探活 Qdrant：复用单例的 ensure_connected（自带重启重连逻辑）。"""
    try:
        from app.repo.qdrant_repo import get_qdrant_repo

        repo = get_qdrant_repo()
        ok = await asyncio.wait_for(repo.ensure_connected(), timeout=3.0)
        return bool(ok), "ok" if ok else "not connected"
    except Exception as exc:                              # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


# ---------------------------------------------------------------------------
# 业务积压（gauge）
# ---------------------------------------------------------------------------

async def _gauge_outbox_pending() -> int | None:
    """outbox 待发事件数。积压持续增长 = relay 或 Kafka 出问题。"""
    try:
        from app.repo.pg import get_pool

        pool = await asyncio.wait_for(get_pool(), timeout=2.0)
        row = await asyncio.wait_for(
            pool.fetchval(
                "SELECT COUNT(*) FROM outbox_events WHERE status = 'PENDING'"
            ),
            timeout=2.0,
        )
        return int(row) if row is not None else None
    except Exception:                                     # noqa: BLE001
        logger.debug("gauge outbox_pending failed", exc_info=True)
        return None


async def _gauge_reconcile_pending() -> int | None:
    """memories 里等待重跑 embed+Qdrant 的行数。"""
    try:
        from app.repo.pg import get_pool

        pool = await asyncio.wait_for(get_pool(), timeout=2.0)
        row = await asyncio.wait_for(
            pool.fetchval(
                "SELECT COUNT(*) FROM memories "
                "WHERE sync_status = 'pending' AND is_active = true"
            ),
            timeout=2.0,
        )
        return int(row) if row is not None else None
    except Exception:                                     # noqa: BLE001
        logger.debug("gauge reconcile_pending failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/healthz")
async def healthz():
    """liveness：进程活着即 200，不探活依赖。"""
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - metrics._started_at),
    }


@router.get("/readyz")
async def readyz(response: Response):
    """readiness：并发探活所有依赖；关键依赖不可用 → 503。

    关键依赖 = PG + Redis（缺了对话写不进去）。
    Qdrant 单独列出：记忆检索可降级，不该因为它挡住整个服务。
    """
    pg_ok, pg_msg = await _check_pg()
    redis_ok, redis_msg = await _check_redis()
    qdrant_ok, qdrant_msg = await _check_qdrant()

    deps = {
        "postgres": {"ok": pg_ok, "detail": pg_msg},
        "redis": {"ok": redis_ok, "detail": redis_msg},
        "qdrant": {"ok": qdrant_ok, "detail": qdrant_msg},
    }

    ready = pg_ok and redis_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "degraded",
        "deps": deps,
    }


@router.get("/metrics")
async def get_metrics(
    x_metrics_token: str = Header(None, alias="X-Metrics-Token"),
):
    """指标快照。

    包含：
      - 计数器：HTTP 请求、LLM 调用/token、工具调用（按状态分）
      - 直方图：LLM 耗时、工具耗时（count/avg/p50/p95/max）
      - 瞬时值：outbox 积压、reconcile 待处理、工具注册数
    """
    if not getattr(settings, "METRICS_ENABLED", True):
        raise HTTPException(status_code=404, detail="metrics disabled")

    expected = getattr(settings, "METRICS_TOKEN", "") or ""
    if expected and x_metrics_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid metrics token",
        )

    snapshot = metrics.snapshot()
    snapshot["gauges"] = {
        "outbox_pending": await _gauge_outbox_pending(),
        "reconcile_pending": await _gauge_reconcile_pending(),
    }
    try:
        from app.tool.registry import TOOL_DEFINITIONS, TOOL_METAS

        snapshot["gauges"]["tools_total"] = len(TOOL_METAS)
        snapshot["gauges"]["tools_enabled"] = len(TOOL_DEFINITIONS)
    except Exception:                                     # noqa: BLE001
        pass

    return snapshot
