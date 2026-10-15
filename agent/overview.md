# 交付概览：Agent System

## 交付 1：全量代码评审（10-09 上午）

- `code-review-2026-10-09.md` —— 4 阻塞 / 9 重要 / 10 建议 / 6 个扩展方向

## 交付 2：工具层插件化 + 执行护栏（10-09 下午）

- `app/tool/spec.py` / `guard.py`（新增）、`registry.py` 重写为自动发现、`tool_executor.py` 只留参数装配
- 7 个工具带 `TOOL_SPEC` + `TOOL_CONFIG`；84 项离线断言全绿（`_test_tool_layer.py`）
- 顺带补齐 `LLM_TIMEOUT_SECONDS`（缺失会让 import 直接崩）

## 交付 3：B / E / F / D-溯源（10-09 傍晚）

### B 并行工具调用
`agent.py` 本轮多工具改 `asyncio.gather` 并发；`TOOL_PARALLEL_ENABLED` 开关 + `TOOL_MAX_PARALLEL`(4) 上限。

### E 可观测性（零新依赖）
- `app/core/obs.py`：trace_id（contextvar + `trace_scope`）+ MetricsRegistry（counter/histogram/分位数）
- `app/api/ops.py`：`/healthz`（liveness 不探依赖）、`/readyz`（探 PG/Redis/Qdrant）、`/metrics`（JSON）
- `main.py`：HTTP 中间件分配/透传 `X-Trace-Id`，日志加 `trace=%(trace_id)s`
- 埋点：LLM 调用/耗时/token、工具调用/耗时（按状态）、outbox 与 reconcile 积压 gauge

### F 工程化基建
`.env.example`、`pyproject.toml`(ruff+mypy+pytest)、`.github/workflows/ci.yml`、`Dockerfile`(多阶段+非root)、
`docker-compose.app.yml`(depends_on: service_healthy)、Kafka healthcheck、`migrations/`(9 个版本) + migrator。

### D 记忆溯源
`GET /memories/{id}/source`：来源消息 + 抽取模型 + prompt 版本。

## 交付 4：并行工具变慢的诊断与修复（10-09 晚）

用户 query：「广州天气 + 22 的 5000 次方 + 介绍琶洲岛」→ 明显变慢。

### 结论：并行代码没问题，慢在别处

实测并发加速比 **3.00x**（3 个 200ms 工具：串行 605ms → 并发 201ms），
`asyncio.gather` 分支逻辑正确。

### 三个真因

| # | 问题 | 状态 |
|---|---|---|
| 1 | `calculator("22**5000")` 结果有 **6713 位**，CPython 3.11+ 限制 `int→str` 最多 4300 位 → 抛 `ValueError` → 工具返回"计算失败" → 模型不知道原因，反复换工具重试（calculator → code_runner → calculator…），每轮一次完整 LLM 往返，直到 `MAX_TOOL_ROUNDS=10` | ✅ 已修：超阈值降级为科学计数法提示 |
| 2 | Exa 联网搜索**单次实测 33.21 秒** —— 外部 API 固有延迟，并行只能省掉"叠加"、省不掉"最大值" | ⚠️ 需决策：降 top_k / 换更快模式 / 缩短超时快速降级 |
| 3 | 若模型**一轮只返回 1 个 tool_call**，并行分支根本不触发，退化为串行多轮 | 🔍 待验证：看日志里 3 个 tool.start 是否同时出现 |

### 修复内容

1. **`app/tool/calculator.py`**：结果转字符串前用 `bit_length` 估算位数（不真正转字符串，避免触发限制），超限返回可读提示。工具不再因"结果太大"而失败。
2. **`app/tool/web_search.py`**：`TOOL_CONFIG.timeout` 30 → 60（原超时必然触发），`retries` 1 → 0（超时后重试只会再吃 60s 且仍失败）。
3. **`app/agent/tool_executor.py` + `app/agent/agent.py`**：新增 `aexecute()` 异步入口，流式路径直接 `await`，**去掉双层线程池**（原来每个工具调用占用 2 个线程：一层 `to_thread` 阻塞 + 一层 guard 线程池，并发时线程池消耗翻倍）。

### 验证

- `_test_tool_layer.py` 84 项通过
- `_test_observability.py` 34 项通过
- `_diag_parallel.py`：并发加速比 3.00x
- `import app.main`：OK，17 条路由（含 `/healthz` `/readyz` `/metrics` `/memories/{id}/source`）

## 待用户拍板

- C 中期：handler 拆独立 worker + Kafka 分发（推荐改用 PG 租约 + Redis Pub/Sub，见 ADR-013）
- D 遗忘曲线（推荐只做检索侧降权，ADR-014）
- D 冲突确认（推荐静默收敛 + 可追溯，ADR-015）
- LLM 成本指标（需要模型单价表，不编造）
