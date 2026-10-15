"""main.py 接线：trace_id 中间件 + 日志格式带 trace + 挂载 ops 路由。
config.py：新增 METRICS_ENABLED / METRICS_TOKEN。
"""

import io

# ---------------------------------------------------------------- config.py
p = "app/core/config.py"
s = io.open(p, encoding="utf-8").read()
anchor = "    # 审计日志是否落参数内容（含用户隐私，默认只记参数名与长度）\n    TOOL_AUDIT_LOG_ARGS :bool = False\n"
assert anchor in s, "config anchor missing"
if "METRICS_ENABLED" not in s:
    s = s.replace(anchor, anchor + """
    # -------- 可观测性 --------
    # 是否开启 /metrics 端点（关掉后返回 404）
    METRICS_ENABLED :bool = True
    # 访问 /metrics 需要的请求头 X-Metrics-Token；留空表示不校验
    # （生产建议配上，或在 Go 网关层直接禁掉 /metrics /readyz 的外部访问）
    METRICS_TOKEN :str = ""
""")
    io.open(p, "w", encoding="utf-8").write(s)
    print("[ok] config.py + METRICS_*")
else:
    print("[skip] config.py already has METRICS_*")

# ---------------------------------------------------------------- main.py
p = "app/main.py"
s = io.open(p, encoding="utf-8").read()

# 1) import
old_import = "from app.api.tasks import router as tasks_router\n"
new_import = (
    "from app.api.tasks import router as tasks_router\n"
    "from app.api.ops import router as ops_router\n"
    "from app.core.obs import (\n"
    "    M_HTTP_REQUESTS,\n"
    "    TRACE_HEADER,\n"
    "    TraceIdFilter,\n"
    "    metrics,\n"
    "    new_trace_id,\n"
    "    reset_trace_id,\n"
    "    set_trace_id,\n"
    ")\n"
)
assert old_import in s, "main import anchor missing"
if "ops_router" not in s:
    s = s.replace(old_import, new_import)

# 2) 日志格式 + filter
old_log = '''logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    force=True,
)
'''
new_log = '''logging.basicConfig(
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
'''
assert old_log in s, "main logging anchor missing"
if "TraceIdFilter()" not in s:
    s = s.replace(old_log, new_log)

# 3) 中间件 + ops router
old_router = "app.include_router(tasks_router)\n"
new_router = '''app.include_router(tasks_router)
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
'''
assert old_router in s, "main router anchor missing"
if "_trace_middleware" not in s:
    s = s.replace(old_router, new_router)

io.open(p, "w", encoding="utf-8").write(s)
print("[ok] main.py trace middleware + ops router")
