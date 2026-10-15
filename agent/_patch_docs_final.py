"""清理一次性补丁脚本 + 更新工程档案（新增 ADR-010/011/012 与待办）。"""

import io
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

for name in [
    "_patch_trace_wiring.py",
    "_patch_parallel_llm.py",
    "_patch_main_ops.py",
    "_patch_infra.py",
    "_patch_memory_source.py",
    "_patch_obs_test.py",
    "_patch_guard_ctx.py",
    "_patch_tools_spec.py",
]:
    path = os.path.join(ROOT, name)
    if os.path.exists(path):
        try:
            os.remove(path)
            print(f"[removed] {name}")
        except Exception as exc:                          # noqa: BLE001
            print(f"[keep] {name}: {exc}")

# ---------------------------------------------------------------- 工程档案
p = os.path.join(ROOT, "project-engineering-file.md")
s = io.open(p, encoding="utf-8").read()

anchor = "- 状态：生效（2026-10-09 交付）\n"
assert anchor in s, "ADR-009 anchor missing"

new_adrs = anchor + """
### ADR-010：可观测性自建，不引入 Prometheus client ★ 本次新增
- 场景：需要 trace_id 贯穿 + 工具/LLM 耗时成功率 + 依赖探活，但不希望为 metrics 引入新依赖和 pull 模型
- 决策：`app/core/obs.py` 提供 contextvar 版 trace_id + 进程内 MetricsRegistry（counter/histogram）；
  `app/api/ops.py` 提供 `/healthz`（liveness，不探依赖）、`/readyz`（探 PG/Redis/Qdrant）、`/metrics`（JSON）
- 理由：当前规模下一个 JSON 端点 + 结构化日志就够用；接 Prometheus 时只改 `snapshot()` 序列化，埋点不动
- 代价：进程内状态，多副本各算各的（查询要聚合所有副本）；没有持久化，重启清零
- 关键坑：trace_id 存在 contextvar 里，线程池和主事件循环都不继承 → 必须在 ToolGuard 里用 `trace_scope` 恢复
- 状态：生效

### ADR-011：本轮多个工具调用并发执行 ★ 本次新增
- 场景：LLM 一轮返回多个独立 tool_calls（如同时查天气和当前时间），串行执行白白叠加延迟
- 决策：`agent.py` 用 `asyncio.gather` + `asyncio.to_thread` 并发；`TOOL_PARALLEL_ENABLED` 开关 +
  `TOOL_MAX_PARALLEL` 上限（超过退回串行）
- 理由：实测两个 300ms 工具 串行 ~0.6s → 并发 ~0.3s；工具间无依赖是常态
- 代价：
  1. 并发路径无法逐个检查 cancel_token（只能在整批前后检查）→ 取消粒度变粗；
  2. 并发不绕过 ToolGuard 的任何保护（超时/重试/熔断/配额都在 guard 里，各自独立生效）
- 顺序保证：tool.start 一次性全发；结果**按 tool_calls 原始顺序**回填 messages 与 tool.result 事件，
  保证 tool_call_id 与结果一一对应（OpenAI 要求 tool message 与 assistant.tool_calls 对应）
- 状态：生效

### ADR-012：迁移用零依赖 migrator，不用 Alembic ★ 本次新增（偏离原建议，理由见下）
- 场景：根目录散着 6 个 `_migrate_*.sql` + 2 个 `public*.sql`，无版本管理
- 决策：`app/repo/migrator.py`（约 150 行）+ `migrations/V0xx__*.sql`；`plan / apply / stamp / history` 四个命令
- 理由：Alembic 的核心价值 autogenerate 依赖 SQLAlchemy metadata，而本项目全程裸 SQL + asyncpg、
  **没有 ORM 模型**，autogenerate 用不上；为了它引入 alembic + sqlalchemy[asyncio] 两个依赖不划算
- 代价：没有自动 diff、没有 downgrade 自动化（down 迁移要自己写）
- 首次接入步骤（重要）：现有库已手动执行过这些脚本 → 先 `stamp V009__tasks.sql` 再 apply，否则重复 DDL 报错
- 状态：生效（原文件 `_*migrate*.sql` 保留未删，migrations/ 是复制）
"""
s = s.replace(anchor, new_adrs, 1)

# 模块表追加
table_anchor = "| 工具执行器 `agent/tool_executor.py` | 已完成（重构） | 只做参数装配，执行交给 guard |\n"
assert table_anchor in s, "table anchor missing"
s = s.replace(table_anchor, table_anchor +
    "| **可观测性 `core/obs.py`（新）** | **已完成** | trace_id(contextvar + trace_scope) + MetricsRegistry(counter/histogram) |\n"
    "| **运维端点 `api/ops.py`（新）** | **已完成** | /healthz、/readyz、/metrics，含依赖探活与积压 gauge |\n"
    "| **迁移器 `repo/migrator.py`（新）** | **已完成** | 零依赖版本迁移 + migrations/ 目录（9 个版本文件） |\n"
    "| 记忆溯源 `api/memory.py` | 已完成 | `GET /memories/{id}/source` 返回来源消息 + 抽取模型/prompt 版本 |\n"
    "| Agent 并行工具 `agent/agent.py` | 已完成 | 本轮多工具并发（TOOL_PARALLEL_ENABLED） |\n"
)

# 待办整段替换
idx = s.find("## 7. 待办")
assert idx > 0, "todo anchor missing"
new_todo = """## 7. 待办

- **T1（第一批，当天）**：移除硬编码密钥并轮换 → 补 `.gitignore` → 修 `task_repo` 启动 → `requirements.txt` 精简无关包（**编码已在本轮修复**）
- **T2（本周）**：修重复 `message.start` → 修 consumer stop 分支 → lifespan 补关闭 → 验证共享池生效
- **T3（下个迭代）**：`code_runner` 加固（R10：改 `sys.executable`、钳制 timeout、`ENABLE_CODE_RUNNER` 开关）→ `message_repo` SQL 语义修正
- **T4（架构演进，需先评审）**：任务 handler 拆独立 worker + `task_event_bus` 走 outbox→Kafka（ADR-008 既定方向）
- **T5（工具层后续）**：trace_id 接入 Kafka 消息 payload（当前 consumer 侧只在内存恢复）→ 熔断/配额换 Redis 后端 → 并行工具的取消粒度优化
- **T6（记忆系统，需产品决策）**：
  - 遗忘曲线：按 importance + last_accessed_at 衰减（**会改变检索结果，需先定公式和阈值并做 A/B 验证**）
  - 冲突确认：slot 收敛目前静默保留最新，改为冲突时向用户确认一次（**需要前端交互契约**）
  - 溯源接口已交付（`GET /memories/{id}/source`），差前端入口
- **T7（工程化，本轮已交付骨架）**：`.env.example` / `pyproject.toml`(ruff+mypy+pytest) / `.github/workflows/ci.yml` /
  `Dockerfile` / `docker-compose.app.yml`(depends_on healthy) / `migrations/` + migrator
  → 下一步：把 5 个 `_test_*.py` 正式改造成 pytest 用例（现在靠 `tests/test_legacy_scripts.py` 调 main() 收编）、
  把 mypy 的 continue-on-error 去掉
- **T8（可观测性后续）**：LLM token 成本核算（需要模型单价表，未做——未拿到官方价格前不编造）、
  多副本指标聚合、`/metrics` 接入 Grafana 或 Prometheus exporter
"""
s = s[:idx] + new_todo

io.open(p, "w", encoding="utf-8").write(s)
print("[ok] project-engineering-file.md updated")
