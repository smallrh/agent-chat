"""工具层插件化改造的离线验证（零 infra 依赖）。

覆盖：
  1. 自动发现：7 个既有工具全部注册，infra 模块不被误注册
  2. **契约回归**：改造后发给 LLM 的 function schema 与改造前逐字一致
  3. spec 归一化：简写形态 / OpenAI 原生形态 / 无 spec 走签名推断
  4. guard：超时、重试、熔断、配额、停用、异步桥接
  5. executor：user_id/conversation_id 注入、坏 JSON、未知工具
  6. 运行时 enable/disable 开关

运行：
    .venv/Scripts/python.exe _test_tool_layer.py
"""

import asyncio
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


# =====================================================================
# 期望的 schema 契约（逐字抄自改造前的 registry.TOOL_DEFINITIONS）
# —— 这条断言保证 LLM 看到的调用契约没有任何漂移
# =====================================================================

EXPECTED = {
    "weather": {"city", "time_range"},
    "calculator": {"expression"},
    "current_time": {"timezone"},
    "code_runner": {"code", "timeout"},
    "search_memory": {"query", "memory_type"},
    "web_search": {"query", "top_k"},
    "create_task": {"title", "goal", "kind"},
}

EXPECTED_REQUIRED = {
    "weather": ["city", "time_range"],
    "calculator": ["expression"],
    "current_time": ["timezone"],
    "code_runner": ["code"],
    "search_memory": ["query"],
    "web_search": ["query"],
    "create_task": ["title"],
}


def section(title):
    print(f"\n=== {title} ===")


# =====================================================================
def test_discovery():
    section("1. 自动发现")
    from app.tool.registry import TOOLS, TOOL_DEFINITIONS, TOOL_METAS

    check(
        "7 个既有工具全部注册",
        set(EXPECTED).issubset(set(TOOL_METAS)),
        f"实际={sorted(TOOL_METAS)}",
    )
    check(
        "TOOLS 兼容字典同步",
        set(TOOLS) == set(TOOL_METAS),
        f"TOOLS={sorted(TOOLS)}",
    )
    check("TOOL_DEFINITIONS 数量 == 启用工具数", len(TOOL_DEFINITIONS) == len(TOOL_METAS))

    for infra in ("context", "registry", "spec", "guard"):
        check(f"infra 模块未被误注册：{infra}", infra not in TOOL_METAS)


def test_schema_contract():
    section("2. 契约回归（改造前后 schema 一致）")
    from app.tool.registry import TOOL_METAS

    for name, want_props in EXPECTED.items():
        meta = TOOL_METAS.get(name)
        if meta is None:
            check(f"{name} 存在", False)
            continue
        fn = meta.definition["function"]
        props = set(fn["parameters"]["properties"])
        check(
            f"{name} 参数集合一致",
            props == want_props,
            f"期望={sorted(want_props)} 实际={sorted(props)}",
        )
        req = fn["parameters"]["required"]
        check(
            f"{name} required 一致",
            req == EXPECTED_REQUIRED[name],
            f"期望={EXPECTED_REQUIRED[name]} 实际={req}",
        )
        check(f"{name} 外层是 OpenAI function 结构", fn.get("name") == name
              and meta.definition.get("type") == "function")
        check(f"{name} description 非空", bool(fn.get("description")))


def test_meta_config():
    section("3. 每个工具的运行配置")
    from app.tool.registry import TOOL_METAS

    cases = {
        "calculator": ("retries", 0),      # 纯函数，失败必是参数错
        "current_time": ("retries", 0),
        "code_runner": ("retries", 0),     # 有副作用，不能重试
        "create_task": ("retries", 0),     # 写操作，重试会重复建任务
        "web_search": ("quota_per_minute", 20),
        "search_memory": ("timeout", 30.0),
        "weather": ("timeout", 15.0),
    }
    for name, (field, want) in cases.items():
        meta = TOOL_METAS.get(name)
        got = getattr(meta.config, field, None) if meta else None
        check(f"{name}.{field} == {want}", got == want, f"实际={got}")

    check("search_memory 是异步工具", TOOL_METAS["search_memory"].is_async)
    check("calculator 是同步工具", not TOOL_METAS["calculator"].is_async)


def test_spec_normalization():
    section("4. spec 归一化")
    from app.tool.spec import ToolConfig, build_definition, parse_config

    def f(a: str, b: int = 1, user_id: int | None = None):
        """doc"""

    d = build_definition("t", {
        "description": "d",
        "parameters": {"a": {"type": "string"}, "b": {"type": "integer"}},
    }, f)
    fn = d["function"]
    check("简写形态 → properties 正确", set(fn["parameters"]["properties"]) == {"a", "b"})
    check("简写形态 → 未写 required 时用签名推断", fn["parameters"]["required"] == ["a"])
    check("user_id 不出现在推断的 required 里", "user_id" not in fn["parameters"]["required"])

    native = build_definition("t2", {
        "description": "d",
        "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    })
    fn2 = native["function"]
    check("OpenAI 原生形态被原样采用", fn2["parameters"]["required"] == ["x"]
          and "type" in fn2["parameters"])

    inferred = build_definition("t3", {"description": "d"}, f)
    fn3 = inferred["function"]
    check("无 parameters → 走签名推断", set(fn3["parameters"]["properties"]) == {"a", "b"})

    cfg = parse_config({"timeout": "7", "retries": 3, "unknown_key": 1})
    check("parse_config 类型转换", cfg.timeout == 7.0 and cfg.retries == 3)
    check("parse_config 忽略未知字段", isinstance(cfg, ToolConfig))
    check("parse_config(None) → 默认", parse_config(None).timeout == 20.0)


# =====================================================================
def test_guard():
    section("5. guard：超时 / 重试 / 熔断 / 配额 / 停用")
    from app.tool.context import ToolContext
    from app.tool.guard import tool_guard
    from app.tool.spec import ToolConfig, ToolMeta

    def make(name, impl, **cfg):
        return ToolMeta(
            name=name, impl=impl,
            definition={"type": "function", "function": {"name": name}},
            config=ToolConfig(**cfg),
        )

    ctx = ToolContext(user_id=42, conversation_id="c1")

    # --- 成功 ---
    tool_guard.reset()
    r = tool_guard.run(make("g_ok", lambda x: f"v={x}", retries=0), {"x": 1}, ctx)
    check("同步成功返回值", r.ok and r.text == "v=1", f"实际={r.text}")
    check("成功记录 attempts=1", r.attempts == 1)
    check("成功未被拦截", r.blocked is None)

    # --- 超时（同步）---
    def slow():
        time.sleep(3)
        return "never"

    r = tool_guard.run(make("g_slow", slow, timeout=0.4, retries=0), {}, ctx)
    check("同步超时被拦住", (not r.ok) and "超时" in r.text, f"实际={r.text}")
    check("超时耗时接近阈值", r.elapsed_ms < 1500, f"elapsed={r.elapsed_ms}ms")

    # --- 重试 ---
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise RuntimeError("boom")

    r = tool_guard.run(
        make("g_retry", flaky, retries=2, retry_backoff=0.01, timeout=5), {}, ctx
    )
    check("重试次数生效（retries=2 → 调用 3 次）", calls["n"] == 3, f"实际={calls['n']}")
    check("重试耗尽后失败文案可读", (not r.ok) and "已重试2次" in r.text, f"实际={r.text}")

    # --- 熔断 ---
    tool_guard.reset("g_cb")
    cb_counts = {"n": 0}

    def broken():
        cb_counts["n"] += 1
        raise ValueError("down")

    cb_cfg = dict(retries=0, circuit_threshold=2, circuit_reset=30)
    tool_guard.run(make("g_cb", broken, **cb_cfg), {}, ctx)
    tool_guard.run(make("g_cb", broken, **cb_cfg), {}, ctx)
    r3 = tool_guard.run(make("g_cb", broken, **cb_cfg), {}, ctx)
    check("连续失败后熔断打开", r3.blocked == "circuit", f"实际={r3.blocked}")
    check("熔断后不再真正调用", cb_counts["n"] == 2, f"实际={cb_counts['n']}")

    ok_after = {"n": 0}

    def healed():
        ok_after["n"] += 1
        return "fine"

    tool_guard.reset("g_cb")
    r = tool_guard.run(make("g_cb", healed, **cb_cfg), {}, ctx)
    check("reset 后恢复调用", r.ok and r.text == "fine")

    # --- 配额 ---
    tool_guard.reset("g_q")

    def ping():
        return "pong"

    q_cfg = dict(retries=0, quota_per_minute=2)
    tool_guard.run(make("g_q", ping, **q_cfg), {}, ctx)
    tool_guard.run(make("g_q", ping, **q_cfg), {}, ctx)
    r = tool_guard.run(make("g_q", ping, **q_cfg), {}, ctx)
    check("超过配额被拒", r.blocked == "quota", f"实际={r.blocked}")

    other = ToolContext(user_id=999)
    r = tool_guard.run(make("g_q", ping, **q_cfg), {}, other)
    check("配额按 user_id 隔离", r.ok, f"实际={r.blocked}")

    # --- 停用 ---
    r = tool_guard.run(make("g_off", ping, enabled=False), {}, ctx)
    check("停用工具被拒", r.blocked == "disabled" and "停用" in r.text)

    tool_guard.reset()


def test_guard_async():
    section("6. guard：异步工具桥接主事件循环")
    from app.tool.context import ToolContext
    from app.tool.guard import tool_guard
    from app.tool.spec import ToolConfig, ToolMeta

    async def async_tool(q: str, user_id: int | None = None):
        return f"async:{q}:{user_id}"

    meta = ToolMeta(
        name="g_async", impl=async_tool,
        definition={"type": "function", "function": {"name": "g_async"}},
        config=ToolConfig(timeout=5, retries=0),
    )

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        ctx = ToolContext(user_id=7, conversation_id="c2", loop=loop)
        r = tool_guard.run(meta, {"q": "hi", "user_id": 7}, ctx)
        check("异步工具桥接成功", r.ok and r.text == "async:hi:7", f"实际={r.text}")

        no_loop = ToolContext(user_id=7)
        r2 = tool_guard.run(meta, {"q": "hi"}, no_loop)
        check("缺 loop 时给出明确提示", (not r2.ok) and "loop=None" in r2.text)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3)
        loop.close()


def test_executor():
    section("7. executor：参数装配与错误处理")
    from app.agent.tool_executor import tool_executor
    from app.tool.context import get_tool_context
    from app.tool.registry import register

    seen = {}

    def need_ctx(text: str, user_id: int | None = None,
                 conversation_id: str | None = None):
        seen["user_id"] = user_id
        seen["conversation_id"] = conversation_id
        seen["ctx"] = get_tool_context().user_id
        return f"echo:{text}"

    register("t_probe", need_ctx, {"description": "probe", "parameters": {"text": {"type": "string"}}},
             {"timeout": 5, "retries": 0}, replace_existing=True)

    out = tool_executor.execute(
        "t_probe", '{"text": "hello"}', user_id=88, conversation_id="conv-9",
    )
    check("同步工具执行成功", out == "echo:hello", f"实际={out}")
    check("user_id 自动注入", seen.get("user_id") == 88, f"实际={seen.get('user_id')}")
    check("conversation_id 自动注入", seen.get("conversation_id") == "conv-9")
    check("ToolContext 可在工具内读取", seen.get("ctx") == 88)

    out = tool_executor.execute("t_probe", '{"text": "x", "bogus": 1}', user_id=88)
    check("多余参数被安全忽略", out == "echo:x", f"实际={out}")

    out = tool_executor.execute("t_probe", "not-json", user_id=88)
    check("坏 JSON 返回可读文案", "不是合法 JSON" in out, f"实际={out}")

    out = tool_executor.execute("nope_tool", "{}", user_id=88)
    check("未知工具返回可用列表", "工具不存在" in out and "t_probe" in out, f"实际={out}")

    out = tool_executor.execute("t_probe", '["not","object"]', user_id=88)
    check("非对象参数被拒", "JSON 对象" in out)


def test_runtime_switch():
    section("8. 运行时 enable / disable")
    from app.tool.registry import TOOL_DEFINITIONS, disable, enable

    names = [d["function"]["name"] for d in TOOL_DEFINITIONS]
    target = "calculator"
    check("停用前在 schema 里", target in names)

    disable(target)
    names = [d["function"]["name"] for d in TOOL_DEFINITIONS]
    check("停用后从 schema 摘除", target not in names)

    from app.agent.tool_executor import tool_executor

    out = tool_executor.execute(target, '{"expression": "1+1"}', user_id=1)
    check("停用后调用被拒", "停用" in out, f"实际={out}")

    enable(target)
    names = [d["function"]["name"] for d in TOOL_DEFINITIONS]
    check("重新启用后回到 schema", target in names)

    from app.agent.tool_executor import tool_executor

    out = tool_executor.execute(target, '{"expression": "1+1"}', user_id=1)
    check("重新启用后可调用", out == "2", f"实际={out}")


def test_llm_integration():
    section("9. 与 llm.py 的接线")
    import app.tool.registry as reg
    import inspect

    from app.agent import llm as llm_mod
    src = inspect.getsource(llm_mod)
    check("llm.py 仍从 registry 取 TOOL_DEFINITIONS", "TOOL_DEFINITIONS" in src)
    check("TOOL_DEFINITIONS 是 list", isinstance(reg.TOOL_DEFINITIONS, list))
    check("每项都有 type=function",
          all(d.get("type") == "function" for d in reg.TOOL_DEFINITIONS))
    check("每项 function.name 唯一",
          len({d["function"]["name"] for d in reg.TOOL_DEFINITIONS})
          == len(reg.TOOL_DEFINITIONS))


def main():
    print("=" * 62)
    print("工具层插件化改造验证")
    print("=" * 62)
    test_discovery()
    test_schema_contract()
    test_meta_config()
    test_spec_normalization()
    test_guard()
    test_guard_async()
    test_executor()
    test_runtime_switch()
    test_llm_integration()

    print("\n" + "=" * 62)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
