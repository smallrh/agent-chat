"""可观测性 / 并行工具 / 迁移器的离线验证（零 infra 依赖）。

覆盖：
  1. trace_id 在线程池里能被恢复（这是 contextvars 最容易踩的坑）
  2. metrics 计数器 + 直方图分位数
  3. 工具并发执行确实省时间（两个慢工具串行 1s+ → 并发接近 0.5s）
  4. /healthz /readyz /metrics 三个路由已挂载
  5. 新增配置项齐全
  6. 迁移器能发现 migrations/ 下的版本文件

运行：
    .venv/Scripts/python.exe _test_observability.py
"""

import asyncio
import concurrent.futures
import inspect
import sys
import threading
import time

FAILED = 0
PASSED = 0


def check(desc, cond, extra=""):
    global FAILED, PASSED
    if cond:
        PASSED += 1
        print(f"  [ok] {desc}")
    else:
        FAILED += 1
        print(f"  [FAIL] {desc} {extra}")


def section(title):
    print(f"\n=== {title} ===")


# =====================================================================
def test_trace():
    section("1. trace_id 跨线程恢复")
    from app.core.obs import get_trace_id, new_trace_id, set_trace_id, trace_scope

    tid = new_trace_id()
    check("new_trace_id 返回 16 位", len(tid) == 16, f"实际={tid}")

    token = set_trace_id(tid)
    check("主线程可读", get_trace_id() == tid)

    # 线程池**不会**继承 contextvars —— 这是关键前提
    def read_raw():
        return get_trace_id()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        raw = ex.submit(read_raw).result()
    check("裸线程池确实拿不到（前提成立）", raw == "-", f"实际={raw}")

    # 用 trace_scope 恢复后就能拿到
    def read_scoped(t):
        with trace_scope(t):
            return get_trace_id()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        got = ex.submit(read_scoped, tid).result()
    check("trace_scope 在线程池里恢复成功", got == tid, f"实际={got}")

    from app.core.obs import reset_trace_id

    reset_trace_id(token)

    # guard 的实现里确实包了 trace_scope（防止被人改回去）
    src = inspect.getsource(__import__("app.tool.guard", fromlist=["x"]))
    check("guard._sync_call 使用 trace_scope", "trace_scope" in src)
    check("guard._async_call 使用 trace_scope", src.count("trace_scope") >= 2)


def test_metrics():
    section("2. metrics")
    from app.core.obs import metrics

    metrics.reset()
    metrics.inc("t_counter", 1, status="ok")
    metrics.inc("t_counter", 2, status="ok")
    metrics.inc("t_counter", 1, status="error")

    snap = metrics.snapshot()
    check("计数器按标签分组", snap["counters"].get("t_counter|status=ok") == 3,
          f"实际={snap['counters']}")
    check("同 metric 不同标签分开", snap["counters"].get("t_counter|status=error") == 1)

    for i in range(1, 101):
        metrics.observe("t_hist", float(i))
    h = metrics.snapshot()["histograms"]["t_hist"]
    check("直方图 count", h["count"] == 100, f"实际={h['count']}")
    check("p50 合理", 49 <= h["p50"] <= 51, f"实际={h['p50']}")
    check("p95 合理", 94 <= h["p95"] <= 96, f"实际={h['p95']}")
    check("max", h["max"] == 100)
    check("uptime 存在", snap["uptime_seconds"] >= 0)

    metrics.reset()
    check("reset 清空", metrics.snapshot()["counters"] == {})


def test_parallel_execution():
    section("3. 工具并发执行")
    from app.tool.context import ToolContext
    from app.tool.guard import tool_guard
    from app.tool.spec import ToolConfig, ToolMeta

    def slow(ms: int):
        time.sleep(ms / 1000)
        return f"slept {ms}"

    meta = ToolMeta(
        name="p_slow", impl=slow,
        definition={"type": "function", "function": {"name": "p_slow"}},
        config=ToolConfig(timeout=10, retries=0),
    )
    ctx = ToolContext(user_id=1)

    tool_guard.reset()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    async def run():
        # 串行基线
        start = time.perf_counter()
        r1 = await asyncio.to_thread(tool_guard.run, meta, {"ms": 300}, ctx)
        r2 = await asyncio.to_thread(tool_guard.run, meta, {"ms": 300}, ctx)
        serial = time.perf_counter() - start

        # 并发（与 agent.py 的并行分支同一手法）
        start = time.perf_counter()
        results = await asyncio.gather(
            asyncio.to_thread(tool_guard.run, meta, {"ms": 300}, ctx),
            asyncio.to_thread(tool_guard.run, meta, {"ms": 300}, ctx),
        )
        parallel = time.perf_counter() - start
        return serial, parallel, r1, r2, results

    try:
        serial, parallel, r1, r2, results = asyncio.run_coroutine_threadsafe(
            run(), loop
        ).result(timeout=20)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3)
        loop.close()

    check("串行耗时 >= 0.6s", serial >= 0.6, f"实际={serial:.2f}s")
    check("并发耗时 < 0.55s（明显快于串行）", parallel < 0.55,
          f"实际={parallel:.2f}s")
    check("并发两个结果都成功",
          all(r.ok for r in results), f"实际={[r.text for r in results]}")
    check("并发结果各自正确",
          results[0].text == "slept 300" and results[1].text == "slept 300")

    # agent.py 里确实用了 gather（弱断言，防止被改回串行）
    src = inspect.getsource(__import__("app.agent.agent", fromlist=["x"]))
    check("agent.py 使用 asyncio.gather", "asyncio.gather" in src)
    check("agent.py 受 TOOL_PARALLEL_ENABLED 控制",
          "TOOL_PARALLEL_ENABLED" in src)


def test_ops_routes():
    section("4. 运维端点")
    from fastapi import FastAPI

    from app.api.ops import router

    app = FastAPI()
    app.include_router(router)

    # 注意：FastAPI 0.141 里 include_router 加进来的 route 对象不一定有顶层
    # .path 属性（默认路由有），所以断言走 openapi schema，更稳。
    spec = app.openapi()
    paths = set(spec.get("paths") or {})
    check("/healthz 已挂载", "/healthz" in paths, f"实际={sorted(paths)}")
    check("/readyz 已挂载", "/readyz" in paths)
    check("/metrics 已挂载", "/metrics" in paths)

    # main.py 里确实挂了这个 router + trace 中间件
    main_src = inspect.getsource(__import__("app.main", fromlist=["x"]))
    check("main.py include ops_router", "ops_router" in main_src)
    check("main.py 注册 trace 中间件", "_trace_middleware" in main_src)
    check("日志格式带 trace_id", "trace=%(trace_id)s" in main_src)


def test_config():
    section("5. 配置项")
    from app.core.config import settings

    for field, want in [
        ("TOOL_PARALLEL_ENABLED", True),
        ("TOOL_MAX_PARALLEL", 4),
        ("METRICS_ENABLED", True),
    ]:
        got = getattr(settings, field, None)
        check(f"{field} = {want}", got == want, f"实际={got}")

    check("LLM_TIMEOUT_SECONDS 存在（缺失会让 llm.py 崩溃）",
          hasattr(settings, "LLM_TIMEOUT_SECONDS"))


def test_migrator():
    section("6. 迁移器")
    from app.repo.migrator import MIGRATIONS_DIR, discover

    versions = discover()
    check("发现迁移文件", len(versions) >= 9, f"实际={len(versions)}")
    check("版本按文件名排序",
          [v for v, _ in versions] == sorted(v for v, _ in versions))
    check("包含基线建表", any("V001" in v for v, _ in versions))
    check("migrations 目录可用", MIGRATIONS_DIR.endswith("migrations"))


def main():
    print("=" * 62)
    print("可观测性 / 并行 / 迁移器验证")
    print("=" * 62)
    test_trace()
    test_metrics()
    test_parallel_execution()
    test_ops_routes()
    test_config()
    test_migrator()

    print("\n" + "=" * 62)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
