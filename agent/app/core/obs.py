"""可观测性地基：trace_id 贯穿 + 零依赖指标。

## 为什么不引入 Prometheus client

当前阶段只要三件事：能在日志里把一次请求的所有环节串起来、能看出工具/LLM 的
耗时与成功率、能探活依赖。一个进程内的 registry + JSON 端点就够了，
不需要为了 metrics 引入新依赖和一套 pull 模型。

将来要接 Prometheus / OpenTelemetry 时，替换 `MetricsRegistry.snapshot()`
的导出格式即可，埋点代码不用动。

## trace_id 的传播要点

`trace_id` 存在 contextvar 里。**线程池不会继承 contextvars**（和 ToolContext
同样的坑），所以工具链路要显式回填 —— 见 ToolGuard._sync_call / _async_call，
以及 `trace_scope()` 的用法。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterable
from contextvars import ContextVar

logger = logging.getLogger(__name__)

_TRACE_ID: ContextVar[str] = ContextVar("trace_id", default="-")

# 放进 HTTP Header 的名字（和 Go Auth 注入 user 的做法保持一致）
TRACE_HEADER = "X-Trace-Id"


# ---------------------------------------------------------------------------
# trace_id
# ---------------------------------------------------------------------------


def new_trace_id() -> str:
    """生成一个短 trace_id（去掉连字符，日志里更好认）。"""
    return uuid.uuid4().hex[:16]


def get_trace_id() -> str:
    return _TRACE_ID.get()


def set_trace_id(trace_id: str):
    """在当前 task/线程设置 trace_id，返回 token（调用方负责 reset）。"""
    return _TRACE_ID.set(trace_id)


def reset_trace_id(token) -> None:
    _TRACE_ID.reset(token)


class trace_scope:
    """显式在某段代码 / 某个线程里恢复 trace_id。

    用法：
        with trace_scope(tid):
            ...
    """

    def __init__(self, trace_id: str | None):
        self._trace_id = trace_id
        self._token = None

    def __enter__(self) -> str:
        if self._trace_id:
            self._token = _TRACE_ID.set(self._trace_id)
            return self._trace_id
        return get_trace_id()

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            _TRACE_ID.reset(self._token)
            self._token = None


class TraceIdFilter(logging.Filter):
    """给每条 LogRecord 注入 trace_id 字段，配合 formatter 的 %(trace_id)s。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.trace_id = get_trace_id()
        return True


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


class MetricsRegistry:
    """进程内的极简指标收集器（线程安全）。

    - counter  累加计数，`{name}|{labels}` 为一个序列
    - histogram 记录耗时，保留最近 N 个样本用于算分位数

    标签不多（每类固定几个），直接拼进 key，不做 cardinality 控制 ——
    规模到要考虑 cardinality 时，就该换成真正的监控系统了。
    """

    HISTORY = 500

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._histograms: dict[str, deque[float]] = {}
        self._started_at = time.time()

    # --------------------------------------------------------------
    # 埋点 API
    # --------------------------------------------------------------

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            bucket = self._histograms.get(key)
            if bucket is None:
                bucket = deque(maxlen=self.HISTORY)
                self._histograms[key] = bucket
            bucket.append(value)

    @staticmethod
    def _key(name: str, labels: dict) -> str:
        if not labels:
            return name
        rendered = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}|{rendered}"

    # --------------------------------------------------------------
    # 读取
    # --------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            counters = dict(self._counters)
            histograms = {k: list(v) for k, v in self._histograms.items()}

        result: dict = {
            "uptime_seconds": int(time.time() - self._started_at),
            "counters": counters,
            "histograms": {},
        }
        for key, values in histograms.items():
            if not values:
                continue
            ordered = sorted(values)
            result["histograms"][key] = {
                "count": len(ordered),
                "avg": round(sum(ordered) / len(ordered), 2),
                "p50": round(_percentile(ordered, 0.50), 2),
                "p95": round(_percentile(ordered, 0.95), 2),
                "max": round(ordered[-1], 2),
            }
        return result

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


def _percentile(sorted_values: Iterable[float], q: float) -> float:
    values = list(sorted_values)
    if not values:
        return 0.0
    idx = min(len(values) - 1, int(round(q * (len(values) - 1))))
    return values[idx]


metrics = MetricsRegistry()


# ---------------------------------------------------------------------------
# 常用埋点名（集中定义，避免各处拼错）
# ---------------------------------------------------------------------------

M_HTTP_REQUESTS = "http_requests_total"
M_LLM_CALLS = "llm_calls_total"
M_LLM_TOKENS = "llm_tokens_total"
M_LLM_DURATION = "llm_duration_ms"
M_TOOL_CALLS = "tool_calls_total"
M_TOOL_DURATION = "tool_duration_ms"
M_KAFKA_CONSUMED = "kafka_events_consumed_total"
M_OUTBOX_PENDING = "outbox_pending"
M_RECONCILE_PENDING = "reconcile_pending"


# ---------------------------------------------------------------------------
# 便捷计时器
# ---------------------------------------------------------------------------


class timer:
    """统计一段代码的耗时到 histogram（同时把异常也记录下来）。

    用法：
        with timer(M_TOOL_DURATION, tool=name):
            ...
    """

    def __init__(self, metric: str, **labels: str):
        self._metric = metric
        self._labels = labels
        self._started = 0.0

    def __enter__(self):
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed_ms = (time.perf_counter() - self._started) * 1000
        labels = dict(self._labels)
        if exc_type is not None:
            labels["error"] = exc_type.__name__
        metrics.observe(self._metric, elapsed_ms, **labels)
        return False
