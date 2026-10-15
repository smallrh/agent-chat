
## 第三批：B（并行工具）+ E（可观测性）+ F（工程化）+ D-溯源（已完成）

用户从扩展清单里挑了 B/C/D/E/F。C 短期已存在（不用做），其余按"零新依赖 + 可验证"原则交付。

### B 并行工具调用
- `agent.py`：本轮多 tool_calls 改 `asyncio.gather` + `to_thread`；`TOOL_PARALLEL_ENABLED`(true) +
  `TOOL_MAX_PARALLEL`(4，超出退回串行)。
- 顺序：tool.start 一次性全发；结果按 tool_calls 原始顺序回填 messages（OpenAI 要求 tool msg 与
  assistant.tool_calls 对应）。**并发不绕过 ToolGuard**（超时/重试/熔断/配额各自独立生效）。
- 代价：并发路径无法逐个检查 cancel_token，取消粒度变粗（只在整批前后查）。
- 实测：两个 300ms 工具 串行 0.62s → 并发 0.31s。

### E 可观测性（零新依赖，没引 prometheus client）
- `app/core/obs.py`（新）：trace_id（contextvar + `trace_scope`）+ MetricsRegistry（counter/histogram/p50/p95）。
- `app/api/ops.py`（新）：`/healthz`（liveness 不探依赖，避免抖动触发重启）、
  `/readyz`（探 PG/Redis/Qdrant，关键依赖挂了才 503）、`/metrics`（JSON，可用 `METRICS_TOKEN` 保护）。
- `main.py`：HTTP 中间件分配/透传 `X-Trace-Id`（上游传了就沿用），日志 format 加 `trace=%(trace_id)s`
  + root handler 挂 `TraceIdFilter`。
- 埋点：llm 调用次数/耗时/token（**流式拿不到 usage，只有非流式记 token**）、
  工具调用次数/耗时（按 tool+status）、outbox pending / reconcile pending 作为 gauge。
- **坑（重要）**：trace_id 在 contextvar 里，线程池/主事件循环都不继承 →
  必须给 ToolContext 加 `trace_id` 字段 + 在 `ToolGuard._sync_call/_async_call` 用 `trace_scope` 恢复。
  和之前 ToolContext 的坑同源。

### F 工程化
新增 `.env.example`、`pyproject.toml`(ruff+mypy+pytest)、`.github/workflows/ci.yml`、`Dockerfile`(多阶段+非root)、
`docker-compose.app.yml`(depends_on: service_healthy)、`tests/test_legacy_scripts.py`、
`migrations/`(9 个版本文件) + `app/repo/migrator.py`。
改了 `agent-infra/docker-compose.yml`（Kafka 补 healthcheck）、`requirements.txt`（**UTF-16→UTF-8**，B2 修掉）。

- **`tests/test_legacy_scripts.py` 是关键技巧**：不改现有 5 个 `_test_*.py` 一行，
  靠 importlib 加载模块 + 调 `main()` 断言返回 0，直接收编进 pytest。
- **迁移器没用 Alembic（ADR-012，偏离用户原建议）**：Alembic 的 autogenerate 要 SQLAlchemy metadata，
  本项目裸 SQL + asyncpg 无 ORM，用不上；为它引入 alembic + sqlalchemy[asyncio] 不划算。
  自研 150 行 migrator 支持 plan/apply/stamp/history。**现有库必须先 stamp V009 再 apply，否则重复 DDL 报错。**

### D 溯源（只做了溯源，其余留给下一轮）
`GET /memories/{id}/source`：来源消息内容 + extract_model/prompt_version/embedding_model。
**遗忘曲线 + 冲突确认没做**：前者会改变检索结果（要先定公式阈值 + 验证），后者需要前端交互契约。

### 验证
`_test_observability.py` 34 项全过；`_test_tool_layer.py` 84 项回归通过；
`import app.main` 冒烟 OK，17 条路由（含 /healthz /readyz /metrics /memories/{id}/source）。
- 注意 FastAPI 0.141：`include_router` 加进来的 route 对象**没有顶层 .path 属性**（默认路由有），
  断言路由要用 `app.openapi()["paths"]`，别用 `app.routes`。

### 遗留
- 根目录留了 7 个 `_patch_*.py` 一次性脚本，沙箱 safe-delete 拦截删不掉（回收站不可用），需手动删。
- CI/Docker 没实跑（本机无 docker、未推送 GitHub）。
- requirements.txt 只做了编码修复，**没精简** Django 等无关包（怕破坏正在运行的环境复现）。
- LLM 成本指标没做（需要模型单价表，不编造）。
