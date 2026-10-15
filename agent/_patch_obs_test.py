import io

p = "_test_observability.py"
s = io.open(p, encoding="utf-8").read()

old = '''def test_ops_routes():
    section("4. 运维端点")
    from fastapi import FastAPI

    from app.api.ops import router

    app = FastAPI()
    app.include_router(router)
    paths = {getattr(r, "path", "") for r in app.routes}
    check("/healthz 已挂载", "/healthz" in paths, f"实际={paths}")
    check("/readyz 已挂载", "/readyz" in paths)
    check("/metrics 已挂载", "/metrics" in paths)

    # 中间件：main.py 里挂了 trace 中间件
    main_src = inspect.getsource(__import__("app.main", fromlist=["x"]))
'''

new = '''def test_ops_routes():
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
'''

assert old in s, "anchor missing"
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8").write(s)
print("[ok] test fixed")
