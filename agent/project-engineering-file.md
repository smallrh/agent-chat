# 项目工程档案：Agent System（Python Agent 后端）

## 1. 项目概况

- **一句话目标**：带长期记忆的多工具 AI Agent 后端。Go Auth 服务负责认证并注入 `X-User-Id`，Python 侧只做业务：对话（流式/非流式）、ReAct 工具循环、长期记忆抽取与语义检索、后台多任务。
- **当前阶段**：工程周期地图 ③ 模块实现（工具层插件化重构已交付）↔ ④ 评审与重构并行（首批阻塞项待修）
- **最后更新**：2026-10-09（工具层插件化）

## 2. 技术栈与版本

版本来自 `requirements.txt`（注意：该文件当前是 UTF-16 LE 编码，已列为待修项 B2）

| 组件 | 版本 | 备注 |
|---|---|---|
| Python | 3.13/3.14（项目 .venv） | 启动一律用 `.venv`，禁用 conda py3147 |
| FastAPI | 0.141.1 | 需以官方发布记录核实 |
| uvicorn | 0.53.0 | `RELOAD=true` 热重载开发 |
| asyncpg | 0.31.0 | 多个 repo 各自建池（见风险 R3） |
| redis (redis-py) | 8.1.0 | summary / 最近消息 / profile 缓存 |
| aiokafka | 0.14.0 | 4 个 topic：events / task / reconcile / dlq |
| qdrant-client | 1.19.1 | REST 6333，`prefer_grpc=False`（容器未开 6334） |
| openai SDK | 3.17.0 | Chat（DeepSeek 兼容）+ Embedding（LM Studio） |
| pydantic / pydantic-settings | 2.13.5 / 2.15.0 | |
| 依赖服务 | PG 5432 / Redis 6379 / Kafka 9092 / Qdrant 6333 / LM Studio 1234 | |

启动命令（唯一约定）：
```
D:/work/agent_system/agent/.venv/Scripts/python.exe -m app.main
```

## 3. 架构决策记录（ADR）

> 只增不删。变更时新增一条并注明替代哪条。

### ADR-001：认证外包给 Go Auth，Python 只做 uuid → id 映射
- 场景：跨服务身份传递
- 决策：Go 在 Header 注入可信 `X-User-Id`(uuid)，Python `get_current_user_id` 查 `users` 表转 `users.id`(int8)
- 理由：避免两套认证体系；Header 由代理层强制覆盖，客户端无法伪造
- 代价：每次请求多一次 PG 查询（有独立小连接池）
- 状态：生效

### ADR-002：消息写入走 Outbox Pattern（不直连 Kafka）
- 场景：Kafka 不可用时不能让对话失败
- 决策：Chat API 只写 `outbox_events` 表，后台 `OutboxRelay` 扫描发 Kafka；重试 5 次耗尽后直写 PG 兜底
- 理由：Kafka 宕机时 Chat 照常返回，relay 恢复后自动补发
- 代价：消息可见性有秒级延迟；兜底路径不更新 Redis（已记风险 R6）
- 状态：生效

### ADR-003：PG 为权威存储，Qdrant 只做语义索引
- 场景：记忆的 content 一致性与检索能力分离
- 决策：content 只存 PG，Qdrant 只存向量 + payload，检索后按 `memory_id` 批量回表
- 理由：单一数据源，Qdrant 可随时重建
- 代价：检索多一次回表（用 `ANY($1)` 批量查询摊平）
- 状态：生效

### ADR-004：Profile 键按 slot 而非 memory_type（**关键决策**）
- 场景：籍贯 vs 现居地都属于 `location`，按 memory_type 建键会互相覆盖
- 决策：Profile Redis 键用细粒度 `slot`（`hometown` / `residence_city` / `favorite_food`…），slot 缺失才回退 memory_type
- 理由：修掉"问现居地答成籍贯"这类错误
- 代价：需维护 `PROFILE_SCHEMA_VERSION`（当前 2），改键语义必须 bump
- 状态：生效（替代了 v1 的按 memory_type 建键方案）

### ADR-005：记忆冲突按 slot 收敛，只保留最新
- 场景：同槽位新旧值并存会导致矛盾记忆同时注入
- 决策：`_converge_by_slot` 按 (created_at, score) 排序，同 slot 只留最新
- 理由：避免"北京 vs 深圳"同时注入
- 代价：静默丢弃旧值，暂不询问用户（扩展点：冲突确认）
- 状态：生效，由 `MEMORY_CONVERGE_BY_SLOT` 开关控制

### ADR-006：记忆近似合并 = 向量粗筛 + LLM 精判
- 场景：nomic-embed-text-v1.5 中文区分度差（"讨厌吃辣" vs "喜欢吃辣" cos=1.0）
- 决策：Qdrant 粗筛（阈值 0.60）→ LLM 二元判断 YES/NO → 才 merge
- 理由：纯向量不可靠，已踩坑
- 代价：每条新记忆最多 3 次额外 LLM 调用
- 状态：生效，由 `MEMORY_MERGE_LLM_JUDGE` 开关控制

### ADR-007：异步工具用 run_coroutine_threadsafe 桥接主事件循环
- 场景：工具执行跑在 worker 线程，无法复用绑定主循环的 Qdrant/PG 单例
- 决策：`ToolGuard` 把协程丢回 `ctx.loop` 执行，`future.result(timeout)` 控时
- 理由：`search_memory` 需要复用 `memory_retrieval_service` 单例
- 代价：worker 线程同步阻塞等结果；跨事件循环调试不直观
- 状态：生效（实现迁移到 guard，语义不变）

### ADR-008：后台任务进程内 asyncio 运行 + 进程内事件总线
- 场景：P2 多任务，进度实时推送
- 决策：`TaskService` 用 `asyncio.create_task` 跑 handler，`TaskEventBus`（`conversation_id → set[Queue]`）做 pub/sub
- 理由：单进程单事件循环，内存中转足够，不引入额外中间件
- 代价：**重启即丢任务、DB 里 running 永久卡死；多副本失效**（已记风险 R4/B5）
- 状态：生效（已知取舍，演进方案见待办 T4）

### ADR-009：工具层插件化（自动发现 + 执行护栏）★ 本次新增
- 场景：加一个工具原本要改 3 处（import / TOOLS / TOOL_DEFINITIONS），新工具接入成本高且容易漏
- 决策：
  - `registry.discover()` 用 `pkgutil.iter_modules(TOOL_PACKAGE_PATH)` 扫描 `app/tool/`；
    模块提供 `TOOL_SPEC`（LLM schema）+ 可选 `TOOL_CONFIG`（运行策略）+ 可选 `TOOL_NAME` / `TOOL_IMPL`
  - **必须有显式标记才注册**（`TOOL_SPEC` 或 `TOOL_IMPL` 至少一个），避免把包内普通函数误注册给 LLM
  - 执行策略抽到独立一层 `ToolGuard`：超时 / 重试 / 熔断 / 配额 / 审计
  - `ToolExecutor` 只保留「按函数签名装配参数」这一个职责
- 理由：加工具降为 1 处（新建一个文件）；顺带把原本分散在各工具的错误处理统一成策略配置
- 代价：
  1. 同步工具超时**无法强杀线程**（CPython 限制），只表示"调用方不等了" → 工具必须自带资源上限；
  2. 熔断/配额状态是**进程内**的，多副本时阈值被放大 N 倍（接口留好，可换 Redis 后端）；
  3. 新增间接层，排查链路多一跳
- 关键坑（已修，勿回退）：
  - 子模块里 `__path__` 不存在（`__path__` 只在包的 `__init__` 可见）→ 必须 `from app.tool import __path__`
  - `importlib.import_module(f"{__name__}.{x}")` 是错的，`__name__` 是 `app.tool.registry` → 必须用 `__package__`
  - 线程池不继承 contextvars → 必须在 `_sync_call` / `_async_call` 里用 `tool_context_scope` 显式重建，
    否则工具内 `get_tool_context()` 拿到空上下文（第 1 版实现有此回归，已在测试中发现并修掉）
- 状态：生效（2026-10-09 交付）

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

### ADR-013（拟议）：tasks 用 PG 租约 + Redis Pub/Sub，不引入 Kafka topic
- 场景：任务要支持多副本 + 重启可恢复，但当前量级很小
- 推荐决策：`tasks` 加 `owner` + `lease_expires_at`，用 `FOR UPDATE SKIP LOCKED` 抢租约；
  进度广播用 Redis Pub/Sub **替换**进程内 `TaskEventBus`
- 理由：Kafka 分发要额外带来 topic/consumer/幂等/offset 与"建行但未发出"的一致性问题，
  而 PG 租约约 50 行 SQL 就能同时满足两条真实需求；进度事件是瞬时的，丢得起，不需要持久化
- 代价：租约心跳与回收要自己管；Redis Pub/Sub 无持久化（副本正好断开时丢几条进度事件）
- 触发升级条件：单进程吞吐跟不上 / 单任务耗时分钟级 / 需要跨语言消费
- 状态：**拟议**（等 T1 修完 `task_repo` 后实施）

### ADR-014（拟议）：记忆遗忘只做检索侧降权，不做删除/归档
- 场景：长期记忆库会持续膨胀，但"记住用户"是产品核心价值
- 推荐决策：`score_final = cosine × 0.5^(days_since_access / half_life)`，
  half_life 按 importance 分级（0.8+→180d，0.5~0.8→60d，<0.5→30d，**需用现有数据回测校准**）；
  由 `MEMORY_DECAY_ENABLED` 控制，初值设大、观察后收紧
- 理由：降权改参数即可回退，删除不可逆；配合现有 `MEMORY_SEARCH_SCORE_THRESHOLD`，
  降到阈值以下的记忆自然不注入，效果等同遗忘但数据完整
- 代价：数据只增不减，表会持续变大（归档留到触发条件满足再做）
- 触发归档条件：单用户记忆 > 5000 条 或 检索 p95 明确因数据量上升
- 状态：**拟议**

### ADR-015（拟议）：冲突不改打断式确认，走"静默收敛 + 可追溯"
- 场景：slot 收敛目前静默保留最新，是否要在冲突时问用户
- 推荐决策：保持静默收敛；保留被覆盖的旧值（复用 `merged_into` / `is_active=false`）；
  新增 `GET /memories/{id}/history` 列出同 slot 历史取值；
  仅当同 slot 30 天内变更 ≥ 3 次时在回答末尾附带一句轻量确认
- 理由：记忆是**后台异步抽取**的，发生在对话之后，没有合适的弹窗时机；
  逐条确认会显著打扰用户；成熟产品也是静默更新 + 可查看/删除
- 代价：用户可能长期不知道旧值被覆盖了（靠 history 接口缓解）
- 状态：**拟议**

## 4. 模块清单与进度

| 模块 | 状态 | 说明 |
|---|---|---|
| 启动生命周期 `main.py` | 需修 | 6 步隔离启动 + 优雅关闭；**关闭不彻底**（R5） |
| 认证 `core/auth.py` | 已完成 | `close_pool()` 已写好但**从未被调用** |
| 对话 API `api/chat.py` | 需修 | 流式 `/chat/stream`、非流式 `/chat`、`/chat/cancel`（非流式无取消） |
| 对话服务 `service/chat_service.py` | 需修 | **重复发 message.start**（R1） |
| Agent 核心 `agent/agent.py` | 已完成 | ReAct 多步循环，MAX_TOOL_ROUNDS=10；流式逐 chunk 线程调度有开销 |
| LLM 客户端 `agent/llm.py` | 已完成 | 已加客户端级 + 请求级超时（依赖 `LLM_TIMEOUT_SECONDS`，曾在 config 缺失导致 import 崩，已补齐） |
| **工具注册表 `tool/registry.py`** | **已完成（重构）** | 自动发现 + 兼容 `TOOLS`/`TOOL_DEFINITIONS` + 运行时 enable/disable |
| **工具 schema `tool/spec.py`（新）** | **已完成** | ToolConfig / ToolMeta / 三种 spec 写法归一化 / 签名推断兜底 |
| **工具护栏 `tool/guard.py`（新）** | **已完成** | 超时 / 重试退避 / 熔断 / 按 user 配额 / 审计日志 / reset + stats |
| 工具实现 `tool/*.py`（7 个） | 已完成 | weather / calculator / current_time / code_runner / search_memory / web_search / create_task，各自带 TOOL_SPEC + TOOL_CONFIG |
| 工具执行器 `agent/tool_executor.py` | 已完成（重构） | 只做参数装配，执行交给 guard |
| **可观测性 `core/obs.py`（新）** | **已完成** | trace_id(contextvar + trace_scope) + MetricsRegistry(counter/histogram) |
| **运维端点 `api/ops.py`（新）** | **已完成** | /healthz、/readyz、/metrics，含依赖探活与积压 gauge |
| **迁移器 `repo/migrator.py`（新）** | **已完成** | 零依赖版本迁移 + migrations/ 目录（9 个版本文件） |
| 记忆溯源 `api/memory.py` | 已完成 | `GET /memories/{id}/source` 返回来源消息 + 抽取模型/prompt 版本 |
| Agent 并行工具 `agent/agent.py` | 已完成 | 本轮多工具并发（TOOL_PARALLEL_ENABLED） |
| 消息写入 `service/message_service.py` | 已完成 | Outbox 写入 |
| Kafka `kafka/*` | 需修 | producer/consumer/outbox_relay/memory_worker/reconcile；**consumer 崩溃分支写错**（R2） |
| 记忆抽取 `kafka/memory_worker.py` | 已完成 | 抽取 prompt v2 + 近似合并 + slot 收敛 |
| 记忆检索 `service/retrieval_service.py` | 已完成 | embed → Qdrant → 批量回表 |
| 记忆 CRUD `api/memory.py` | 已完成 | 列表/详情/软删/版本化修改/语义检索 |
| 画像 `service/profile_service.py` | 已完成 | Redis 缓存 + PG 重建，schema v2 |
| 任务 `api/tasks.py` + `service/task_service.py` | **阻塞** | **模块级 `task_repo` 未启动，端点全 500**（B0） |
| 数据层 `repo/*` | 需修 | **start() 非幂等 + 连接池过多**（R3） |

## 5. 接口约定

已确认的契约，实现不得擅自偏离（要改先改本档案再改代码）。

### SSE 事件协议（`/chat/stream`）
事件顺序：`agent.start` → `message.start` → N×(`message.delta` | `tool.start`/`tool.result`/`tool.end`) → `message.end` → `agent.end` → `done`
每个 SSE 帧由 `app/utils/sse.py:sse(event, data)` 编码，自动补 `timestamp`（UTC ISO8601 微秒 + Z）。

### 取消协议
`POST /chat/cancel {run_id}` → `{ok, cancelled}`；`run_id` 由 `agent.start` 事件下发。token 归属校验 `token.user_id != user_id` → 403。

### 任务 API
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tasks` | body: conversation_id, title, goal, kind；返回 task_id |
| GET | `/tasks?conversation_id=&status=&limit=&offset=` | |
| GET | `/tasks/{task_id}` | 非本人 → 403 |
| POST | `/tasks/{task_id}/cancel` | 已终态返回 `{ok:true, cancelled:false, reason:"already terminal"}` |
| GET | `/tasks/stream?conversation_id=` | SSE，事件：task.created/running/progress/completed/failed/cancelled |

**任务 handler 契约**：`async def handler(task_id, progress, conversation_id, user_id, goal, title) -> dict`，
`progress(done: int, total: int, message: str)`；返回值建议含 `summary` 与 `steps`（`_format_result` 依赖这两个键）。

### 记忆 API
`GET /memories`、`GET /memories/{id}`、`DELETE /memories/{id}`（软删级联 PG+Qdrant+Redis）、`PATCH /memories/{id}`（版本化 INSERT + 老行 is_active=false）、`POST /memories/retrieve`。

### 工具层契约（ADR-009）
新增工具只需在 `app/tool/` 建一个文件：

```python
# app/tool/my_tool.py
def my_tool(arg: str, user_id: int | None = None) -> str:
    """工具实现；user_id 由 ToolContext 自动注入，LLM 不需要传。"""
    return f"..."

TOOL_SPEC = {
    "description": "给 LLM 看的说明，写清楚什么时候用",
    "parameters": {"arg": {"type": "string", "description": "参数说明"}},
    "required": ["arg"],
}
TOOL_CONFIG = {"timeout": 20, "retries": 0, "quota_per_minute": 60}
```

规则：
- 函数名 == 工具名（或显式 `TOOL_IMPL`）；模块名 == 工具名是默认行为
- `user_id` / `conversation_id` 形参由执行器自动注入，且**不会**出现在发给 LLM 的 schema 里
- 有副作用的工具（写操作、执行代码）必须 `retries: 0`，否则重试会造成重复执行
- `_SKIP_MODULES = {"__init__", "context", "registry", "spec", "guard"}` 永不注册

## 6. 已知风险

| ID | 风险 | 等级 |
|---|---|---|
| B0 | 模块级 `task_repo` 从未 start → `/tasks/*` 全部 500 | 🔴 |
| B1 | `config.py` 硬编码真实 WEB_SEARCH_API_KEY，文件已被 git 跟踪 | 🔴 |
| B2 | `requirements.txt` 为 UTF-16 LE，pip 无法安装；且混入 Django 等无关包 | 🔴 |
| B3 | 仓库无 `.gitignore`，`.env` 随时可能推上 GitHub（remote: github.com/smallrh/agent-chat） | 🔴 |
| B4 | `code_runner` 任意代码执行 + timeout 无上界 + 解释器走 PATH | 🟠 |
| B5 | 进程重启后 `tasks.status='running'` 永久卡死，无自愈 | 🟠 |
| R1 | `chat_service` 重复发 `message.start` | 🟠 |
| R2 | Kafka consumer 崩溃分支 `self.consumer = None` 后再 stop，永远停不掉 | 🟠 |
| R3 | Repository.start() 非幂等 + 至少 8 个独立 asyncpg 池（≈9×PG_POOL_SIZE 连接） | 🟠 |
| R4 | 任务与事件总线都在进程内，多副本部署失效 | 🟠（已知取舍） |
| R5 | lifespan 关闭不彻底（chat_service / auth / retrieval / profile 的 PG 池都没关），热重载下连接累积 | 🟠 |
| R6 | outbox 兜底直写 PG 不更新 Redis → 会话上下文缺消息 | 🟡 |
| R7 | `message_repo` 用 `DO UPDATE` 而非注释所称的 `DO NOTHING`，Kafka 重放污染 Redis | 🟠 |
| R8 | 无测试框架、无 CI、无 Dockerfile、无健康检查端点；6 个 `_migrate_*.sql` 手工脚本无版本管理 | 🟡 |
| R9 | `app` logger 全局 DEBUG + force=True，生产日志会落用户完整消息内容 | 🟡 |
| R10 | `code_runner` 的 `timeout` 参数未在函数内做上界钳制，LLM 可传超大值；目前靠 guard 外层 15s 兜底，但线程仍会继续跑到 subprocess 自己超时 | 🟠（待修，已在 TOOL_CONFIG 注释标注） |
| R11 | guard 的熔断/配额状态是进程内的，多副本时实际阈值 = 阈值 × 副本数 | 🟡（已知取舍，接口已留） |

## 7. 待办

> 详细推荐方案（含触发条件与执行顺序）见 `recommendations-2026-10-09.md`。
> 结论：先修已知 bug → tasks 租约（ADR-013）→ 遗忘降权（ADR-014）→ 记忆 history（ADR-015）；
> Kafka 分发 / 真归档 / 冲突弹窗默认不做，等触发条件。

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

---

## 8. 问题与修复记录

> 记录真实踩到的坑与处置，避免重复排查。

### 2026-10-09：三连问「天气 + 22^5000 + 琶洲岛」明显变慢

**现象**：一次提问含三个子任务，端到端耗时远超预期。

**排查**：写 `_diag_parallel.py` 分别测「并发 vs 串行」与单工具耗时。

**结论**：并行代码没问题 —— 3 个 200ms 工具，串行 605ms、并发 201ms，加速比 **3.00x**。
（注意：毫秒级工具测不出并发收益，线程调度开销会盖过，必须用 200ms+ 的慢工具测。）

**三个真因**：

1. **calculator 大整数崩溃 → 模型重试风暴**（主因）
   `22**5000` 有 6713 位；CPython 3.11+ 限制 `int→str` ≤ 4300 位，
   `return f"{value}"` 直接抛 `ValueError`，被 `except Exception` 吞成"计算失败"。
   模型看不到根因 → 反复换工具（calculator → code_runner → calculator…），
   每轮一次完整 LLM 往返，最多吃满 `MAX_TOOL_ROUNDS=10`。

   处置：`calculator` 转字符串前用 `bit_length` 估算位数（不真正转字符串，规避限制），
   超限返回「结果是一个约 N 位的整数，超出可展示范围」。

2. **Exa 联网搜索单次 33.21 秒**（外部 API 固有延迟）
   并行省的是"叠加"，省不掉"最大值"——搜索天然最慢，它就是那轮的下限。

   处置：`TOOL_CONFIG.timeout` 30 → 60（原超时必然触发），
   `retries` 1 → 0（超时后重试只会再吃 60s 且仍失败）。

3. **若模型一轮只返回 1 个 tool_call，并行分支不触发** → 退化串行多轮
   待验证：看日志里 3 个 `tool.start` 是否同时出现。

### 2026-10-09：Exa 官方流式写法在本环境崩溃

**现象**：按官方示例 `client.responses.create(..., stream=True)` 调用，HTTP 200 但抛

    JSONDecodeError: Expecting value: line 1 column 1 (char 0)

**根因**：Exa 的 SSE 流里夹了 `: keep-alive` 注释行。openai SDK 的
`openai/_streaming.py` 对该行拿到的 data 缓冲区为空 → `json.loads("")` 崩。
且异常发生在 async generator 内部，**外层 try/except 接不到**。

（用裸 httpx 抓原始流确认：HTTP 200，事件完整，仅多了 `: keep-alive` 注释行。）

**处置**：保持非流式，改为 httpx 直连 Exa Responses API + 手写宽容 SSE 解析
（跳过注释行、空 data、不可解析事件）。原因已写进 `app/tool/web_search.py` 顶部文档。

### 2026-10-09：连打多个补丁脚本把 guard.py 写坏

**现象**：改 `guard.py` 时出现三次连续错误——
① 同步 `run()` 被改成 `await`（SyntaxError）；② 漏 `_describe`；③ 漏 `_blocked`。

**根因**：无 `Edit` 工具，改用「补丁脚本 + python 执行」；
对一个文件做**多处结构性改动**时连打几个补丁，补丁之间互相干扰且难回滚。

**教训**：单文件多处结构性改动 → 宁可先读完整文件再 Write 整体重写，
并且重写前**逐项核对原文件的内容清单**（函数、常量、模块级变量），
不要凭记忆重写。本次就是重写时漏掉了 `TOOL_SPEC`/`TOOL_CONFIG`，
导致 `web_search` 被 registry 跳过。

### 2026-10-09：memories 表缺 slot 列 —— 跨会话记忆失效的真正根因

**现象**：日志出现
```
ERROR | app.service.profile_service | ProfileService.retrieve ... UndefinedColumnError: column "slot" does not exist
```
且长期「新会话记不住用户」。

**根因**：`slot` 是演进期新增列。最新 `public.sql` 的 CREATE TABLE 里**有**这一列，
但已有数据库不会自动补列；散落的六个 `_migrate_*.sql` 手工脚本里**没有一个加过它**。

**影响面**（比看起来大）：
- `MemoryRepository.save()` 的 INSERT 引用 `slot` → **每条记忆都写不进 PG**
- `ProfileService.rebuild_from_memories()` 的 SELECT 引用 `slot` → **每次都崩**，降级返回 `{}`
  → profile 块永远为空 → 新会话拿不到用户事实
- 两处都有 `try/except` 吞异常并降级，只在 DEBUG 日志留痕，所以线上看着「没报错」

**修复**：执行 `migrations/V010__memories_slot.sql`（幂等，含列补齐 + 老数据回填 + 索引）。
已执行过的话重复执行无害。

**警示**：`public.sql` 顶部是 `DROP TABLE IF EXISTS ...` + `CREATE TABLE`，
对**已有数据的库是毁灭性的**，只能在初始化全新库时使用。
后续所有 schema 变更一律走 migrations/ 的版本脚本。

**反思**：如果有 migrator（ADR-012），启动时会发现 V010 未应用并自动补上，
不会静默失效。这个 bug 本身就是"缺迁移管理"的代价。
