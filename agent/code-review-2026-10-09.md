# 代码评审报告：Agent System（Python Agent 后端）

- 评审日期：2026-10-09
- 评审范围：`app/` 全部 48 个 Python 文件 + 配置 + 依赖清单 + Git 仓库状态
- 评审方式：主干链路精读（启动生命周期 → API → Service → Agent/工具 → Repo → Kafka）+ 全局 grep 交叉验证
- 总体判断：**架构分层清晰、降级设计到位、注释质量很高**，但存在 4 个会直接导致功能不可用/密钥泄露的阻塞项，以及一批资源生命周期与并发问题。

---

## 一、先说结论

| 等级 | 数量 | 一句话 |
|---|---|---|
| 🔴 阻塞 | 4 | `/tasks` 全挂、密钥进仓库、依赖文件装不上、`.gitignore` 缺失 |
| 🟠 重要 | 9 | 重复 SSE 事件、consumer 崩溃分支写错、连接池泄漏/翻倍、重启后任务卡死 |
| 🟡 建议 | 10 | 流式线程调度开销、可观测性、测试与 CI、配置治理 |

架构本身没有问题：Outbox + Kafka + 双写 PG/Qdrant 的分层是对的，记忆的 slot 收敛设计（按 slot 而非 memory_type 建键）是这套系统里最有价值的一个决策。问题集中在**资源生命周期管理**和**工程化基建**上。

---

## 二、🔴 阻塞项（建议当天修）

### 1. 模块级单例 `task_repo` 从未启动 → `/tasks` 全部端点 500

- **位置**：`app/repo/task_repo.py:313`（`task_repo = TaskRepository()`）、`app/api/tasks.py:9,63,89,105,126,149`
- **问题**：`main.py` 的 lifespan 只调用了 `task_service.start()`，它启动的是 `TaskService` 内部的 `self._repo = TaskRepository()`（另一个实例）。模块级单例 `task_repo` 没有任何地方调用 `start()`。
- **影响**：`assert_conversation_owner` / `get_task` / `list_tasks` 首行就是 `if not self.pool: raise RuntimeError("PostgreSQL pool is not started")` → `POST /tasks`、`GET /tasks`、`GET /tasks/{id}`、`GET /tasks/stream` 全部 500。
- **验证证据**：全项目 grep `task_repo.start` 零命中。

**修正（推荐方案：让 API 层复用 TaskService 的 repo，消除双实例）**

```python
# app/service/task_service.py
class TaskService:
    @property
    def repo(self) -> "TaskRepository":
        return self._repo
```

```python
# app/api/tasks.py
-from app.repo.task_repo import task_repo
+from app.service.task_service import task_service
+
+def _repo():
+    return task_service.repo

-await task_repo.assert_conversation_owner(...)
+await _repo().assert_conversation_owner(...)
```

**备选（最小改动）**：在 `main.py` lifespan STEP 6 加一步：

```python
from app.repo.task_repo import task_repo
try:
    await task_repo.start()
    logger.warning(">>> [lifespan] STEP 6.5 task_repo OK ✅")
except Exception:
    logger.exception(">>> [lifespan] STEP 6.5 task_repo FAILED ❌")
```

---

### 2. 真实 API Key 硬编码进已跟踪文件（安全红线）

- **位置**：`app/core/config.py:32`

```python
WEB_SEARCH_API_KEY: str = "tvly-<已泄露·已轮换·此处涂销>"   # 明文已从代码中移除
```

- **状态（2026-10-09 已处理）**：
  - `app/core/config.py` 默认值已改为 `""`，并写死「密钥类配置一律空默认」约定。
  - 本评审文档里的明文同步涂销，避免第二处副本被提交。
  - **历史核查结论**：`git log -S <key> --all` 与 `origin/main` 均**零命中** —— 密钥只存在于未提交的工作区改动中，**从未进入 git 历史，也从未 push**，无需 history rewrite。
  - **仍需用户执行**：去 Tavily 控制台**轮换该密钥**（按泄露处理），把新值写进 `agent/.env`。
    注意：`.env` 目前**没有** `WEB_SEARCH_API_KEY`/`EXA_API_KEY`，运行期一直靠这个硬编码默认值；
    代码置空后若不补 `.env`，重启后联网搜索会退化为「未配置 Key」提示（不报错，也不阻断对话）。

- **问题**：这是 Tavily 的真实密钥，作为**默认值**写进了被 git 跟踪的源码文件。`git status` 显示该文件已被跟踪且有修改，远端为 `git@github.com:smallrh/agent-chat.git`。
- **影响**：密钥已进入 git 历史；任何人拿到仓库即可使用你的搜索配额。
- **处理步骤**：
  1. 改成 `WEB_SEARCH_API_KEY: str = ""`（所有密钥类配置一律空默认）；
  2. **轮换该密钥**（把默认值写进代码的那一刻就应视为已泄露）；
  3. `git log -p -- app/core/config.py` 确认历史提交里是否也有，若已 push 需要按泄露流程处理。

---

### 3. `requirements.txt` 是 UTF-16 LE 编码，pip 装不上

- **位置**：`requirements.txt`
- **验证**：文件头 4 字节为 `ff fe 61 00`（UTF-16 LE BOM + 'a'）。这是 Windows PowerShell 下 `pip freeze > requirements.txt` 重定向的经典产物。
- **影响**：换机器/换容器 `pip install -r requirements.txt` 会解析失败或装出乱码包名，环境不可复现。
- **附带问题**：清单里混进了大量与本项目无关的包（`Django==6.1.1`、`Booktype==1.5`、`agent-detector==2.0.0`、`sentry-sdk`、`pywin32`、`simplejson`…），明显是全局环境 freeze，不是本项目直接依赖。这会显著拖慢镜像构建并放大攻击面。
- **修正**：

```bash
# 重新生成干净清单（在 .venv 里执行）
.venv/Scripts/python.exe -m pip freeze > /tmp/req.txt
# 或者干脆手写直接依赖 + pip-compile 锁定
```

建议只保留直接依赖，间接依赖交给 `pip-compile`（pip-tools）锁定：

```
fastapi
uvicorn[standard]
pydantic
pydantic-settings
asyncpg
redis
aiokafka
qdrant-client
openai
httpx
```

> 注：`Django==6.1.1` 等版本号请以 PyPI 官方为准核实，我这里不做版本断言。

---

### 4. 仓库无 `.gitignore`，`.env` 随时可能被推上 GitHub

- **位置**：仓库根目录（缺失文件）
- **现状**：`.env` 存在且含 `OPENAI_API_KEY` / `PG_PASSWORD` / `EXA_API_KEY` / `MINIO_SECRET_KEY` 等；当前**侥幸**未被 git 跟踪（grep `git ls-files` 无命中），但没有任何忽略规则拦截。
- **影响**：一次 `git add .` 或 IDE 的 "commit all" 就会把密钥推到公开/私有远端。
- **修正**：立即补 `.gitignore`：

```gitignore
.env
.env.*
!.env.example
.venv/
__pycache__/
*.pyc
.idea/
.workbuddy/
_patch_*.py
```

并同步补一份 `.env.example`（只写键名，不写值）。

---

## 三、🟠 重要项

### 5. 流式链路重复发送 `message.start`

- **位置**：`app/service/chat_service.py:316-323`（try 外）与 `327-334`（try 内）
- **影响**：前端一次对话收到两条 `message.start`，同一个 `message_id`。若前端按此事件初始化消息卡片，会出现重复渲染或状态重置。
- **修正**：删除 try 内的第二次（外层那次保留，它在 `agent.start` 之后语义更合理）。

### 6. Kafka consumer 崩溃分支：先置 None 再 stop，永远 stop 不到

- **位置**：`app/kafka/consumer.py:208-212`

```python
self.consumer = None
try:
    await self.consumer.stop()   # ← NoneType，必抛 AttributeError，被 except 吞掉
except Exception:
    pass
```

- **影响**：崩溃的旧 consumer 实例未被关闭，心跳线程与 socket 泄漏；`self.consumer = None` 后靠 30s 重试重建，期间消息积压。
- **修正**：

```python
old = self.consumer
self.consumer = None
if old is not None:
    try:
        await old.stop()
    except Exception:
        logger.exception("stop broken consumer failed (ignored)")
```

### 7. Repository.start() 非幂等 → 共享实例被"偷换"连接池

- **位置**：`app/repo/memory_repo.py:51`、`app/repo/message_repo.py:19`、`app/repo/redis_repo.py:18`（`self.pool = await create_pool(...)` 无条件覆盖）
- **影响**：`MemoryWorker` 把自己的 `memory_repo` 共享给 `ProfileService`（`memory_worker.py:545`，注释说"复用 PG 连接池"），但 `ProfileService._ensure_pg()` 仍会调 `self.memory_repo.start()` → **新建连接池覆盖掉共享的那个**。结果：旧池连接泄漏、两个组件各用一个池，注释里的优化意图完全落空。
- **修正**（三个 repo 统一改）：

```python
async def start(self):
    if self.pool is not None:
        return
    self.pool = await asyncpg.create_pool(...)
```

### 8. 连接池数量失控（至少 8 个独立 asyncpg 池）

当前每个组件各建各的池：

| 池 | 位置 |
|---|---|
| auth | `app/core/auth.py:31` |
| chat_service._msg_repo | `app/service/chat_service.py:29` |
| conversation_repo | `app/service/conversation_service.py` |
| consumer.message_repo | `app/kafka/consumer.py:53` |
| memory_worker.memory_repo | `app/kafka/memory_worker.py:534` |
| retrieval.memory_repo | `app/service/retrieval_service.py:79` |
| profile.memory_repo | `app/service/profile_service.py:38` |
| task_service._repo + task_repo | `app/service/task_service.py:35` |
| outbox_relay._message_repo | `app/kafka/outbox_relay.py:39` |

每个 `max_size=PG_POOL_SIZE` → PG 侧连接数 ≈ 9 × PG_POOL_SIZE。加上 `RELOAD=true` 热重载会起新旧进程，连接数还会翻倍。

**建议**：抽一个 `app/repo/pg.py` 提供全局单例池，所有 repo 共享；或至少给每类 repo 设独立上限并加总预算。这是本次评审里最值得做的重构，收益（连接数、可观测性、启动速度）明显。

### 9. `code_runner` 任意代码执行 + timeout 无上限

- **位置**：`app/tool/code_runner.py:46-51`
- **问题**：
  1. `subprocess.run(["python", "-I", path])` 走 PATH 查找解释器，热重载/多环境里可能不是当前 venv 的解释器（这正是你之前踩过的"双实例 + 解释器不一致"坑的同款风险）；
  2. `timeout` 由 LLM 传入，schema 里无上界（`registry.py:111`），可传 `999999` 长期占住 worker 线程；
  3. `python -I` 不阻止 `os.system` / socket 外连，本质仍是任意代码执行。
- **修正**：

```python
import sys

def code_runner(code: str, timeout: int = 10) -> str:
    try:
        timeout = max(1, min(int(timeout), 10))
    except (TypeError, ValueError):
        timeout = 10
    ...
    proc = subprocess.run(
        [sys.executable, "-I", path],   # 用当前解释器，不走 PATH
        capture_output=True, text=True, timeout=timeout,
    )
```

- **额外建议**：加配置开关 `ENABLE_CODE_RUNNER: bool = False`，生产默认关闭；真要开放请放进禁用网络的独立容器。

### 10. `message_repo.save_message` 注释与实现不符，重放会污染 Redis

- **位置**：`app/repo/message_repo.py:245-247`
- **问题**：注释写 `ON CONFLICT DO NOTHING`，实际是 `DO UPDATE SET message_id = EXCLUDED.message_id`（空转更新）。
- **影响**：Kafka 重放时 `RETURNING sequence_id` 返回的是**旧行**的 sequence_id，随后 `redis_repo.push_message(event)` 会再 push 一条内容相同的消息进 List → Redis 侧没有幂等，最近 20 条里出现重复。
- **修正**：改回真正的 `DO NOTHING`，并显式处理未插入的情况：

```sql
INSERT INTO messages (...) VALUES (...)
ON CONFLICT (message_id) DO NOTHING
RETURNING sequence_id
```

并在 `sequence_id is None` 时回查一次（当前已有兜底逻辑，保留即可）。同时建议 `push_message` 按 `message_id` 做一次去重（读最新 20 条比对，或改用 `HASH` 结构）。

### 11. 进程重启后 `status=running` 的任务永久卡死

- **位置**：`app/service/task_service.py:38`（`self._running` 是进程内 dict）
- **影响**：任务跑在进程内 `asyncio.create_task`，进程一停任务即消失，但 DB 里那行仍停在 `running`，前端进度条永远转圈，也没有任何自愈逻辑。
- **修正**：启动（`TaskService.start()`）时扫一次 `status='running'` 的旧行，置为 `failed`（error 注明 "interrupted by restart"）并发布 `task.failed` 事件。逻辑与 `outbox_relay.recover_stale_processing()` 完全一致，可以直接照抄那个模式。

### 12. lifespan 关闭不彻底，热重载下 PG 连接持续累积

- **位置**：`app/main.py:196-228`
- **问题**：shutdown 只停了 Kafka 相关组件和 `task_service`；`chat_service._msg_repo`、`auth._pool`、`retrieval.memory_repo`、`profile.memory_repo`（含其 PG 池）都没关。
- **影响**：你用 `RELOAD=true` 热更新开发，每次修改文件都会重建进程，旧连接不释放 → PG 连接数一路涨到打满。这跟你之前遇到的"双实例 + 连接假死"是同一类问题。
- **修正**：在 shutdown 段补上：

```python
try:
    await chat_service.aclose()
except Exception:
    logger.exception("chat_service.aclose() error (ignored)")
try:
    from app.core.auth import close_pool
    await close_pool()
except Exception:
    logger.exception("auth.close_pool() error (ignored)")
```

（`ChatService` 需补一个 `aclose()` 关掉 `_redis` / `_msg_repo`。`close_pool()` 已经写好了，只是没人调用。）

### 13. 非流式 `/chat` 完全没有取消/超时保护

- **位置**：`app/api/chat.py:20`、`app/agent/agent.py:220`
- **问题**：`cancel.py` 的 run token 只在 `stream_chat` 创建，`POST /chat` 拿不到 run_id，也没有 LLM 调用超时。
- **影响**：模型卡住或工具链跑满 10 轮时，请求会一直挂着占住 worker 线程和连接池。
- **建议**：给 `llm.chat` 加 `timeout`（OpenAI SDK 支持 `timeout` 参数，具体签名请以官方 SDK 文档核实），并在非流式路径也 `create_token` + `remove_token`。

---

## 四、🟡 建议项

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 14 | `app/agent/agent.py:386` | 每个流式 chunk 都 `asyncio.to_thread(_sync_next, ...)`，一个 token 一次线程切换，长回复开销明显 | 在工作线程里把整个 stream 消费完写入 `asyncio.Queue`，主循环 `await q.get()`；或直接换 `AsyncOpenAI` 用 `async for` |
| 15 | `app/agent/cancel.py:38` | `_REGISTRY` 无 TTL，异常路径（generator 从未被迭代）会泄漏 | 加定时清理任务，或用 `WeakValueDictionary` + 时间戳 |
| 16 | `app/kafka/outbox_relay.py:207` | 重试耗尽后直写 PG 兜底，但不写 Redis → 该消息不进会话上下文 | 兜底路径补一次 `redis_repo.push_message(event)` |
| 17 | `app/agent/tool_executor.py:100` | 异步工具超时硬编码 30s | 提到 settings：`TOOL_ASYNC_TIMEOUT_SECONDS` |
| 18 | `app/service/agent_service.py` | 死代码（实时链路是 `chat_service.py`） | 删除，避免后人改错文件 |
| 19 | 全局 | 无测试框架、无 CI、无 Dockerfile、无 `/healthz` | 见"可扩展点"第 5 节 |
| 20 | `app/main.py:35` | `app` logger 设 DEBUG + `force=True` → 生产会把完整 messages（含用户隐私内容）打进日志 | 按 `ENV` 分档，DEBUG 只开在开发环境；日志脱敏 |
| 21 | `app/core/config.py` | `MAX_TOKENS` 等必填项无默认，缺 env 时报错不友好 | 给合理默认值 + 补 `.env.example` |
| 22 | `app/kafka/memory_trigger.py:8` | 触发正则硬编码在源码里 | 提到配置或规则文件，支持热调 |
| 23 | `app/agent/agent.py:60` | `_estimate_tokens` 是中英文混合的粗略估算 | 够用；若要精确再引入 tiktoken（需核实模型是否支持其编码） |

---

## 五、可扩展的地方（按投入产出排序）

### A. 工具层插件化（价值最高，改动最小）

现在是"加工具要改 3 处"：`registry.py` 的 `TOOLS`、`TOOL_DEFINITIONS`、以及 import。改成自动发现：

```python
# app/tool/registry.py
def _discover():
    import importlib, pkgutil
    for mod in pkgutil.iter_modules(__path__):
        m = importlib.import_module(f"{__name__}.{mod.name}")
        # 约定：模块里定义了 __TOOL_NAME__ 的可调用对象即注册
```

配套可加：工具级超时/重试/熔断、工具调用审计（谁在什么时候调了什么）、按用户配额限流。

### B. 并行工具调用

`agent.py:453` 当前 `for item in tool_calls.values()` 串行执行。对无依赖的工具（如同时查天气和当前时间）可以并发：

```python
results = await asyncio.gather(*[
    asyncio.to_thread(tool_executor.execute, ...) for item in tool_calls.values()
])
```

注意要保持 `tool_call_id` 与结果的一一对应。

### C. 后台任务从"进程内"升级为"可恢复"

当前 `TaskService` 进程内跑，重启丢任务。演进路径：
1. 短期：加 `recover_stale_running()`（见 🟠-11）+ 进度写 DB（已做）；
2. 中期：handler 注册表拆成独立 worker 进程，通过 Kafka topic 领任务，多副本可水平扩展；
3. `task_event_bus` 同步换成"写 outbox → Kafka → 各副本消费 → 本地 bus"（代码注释里已经写了这个设计，方向是对的）。

### D. 记忆系统

- **遗忘曲线**：按 `importance` + `last_accessed_at` 衰减，冷记忆降权或归档，避免长期记忆库无限膨胀拖慢检索；
- **冲突确认**：slot 收敛现在"静默保留最新"，可以升级为检测到冲突时向用户确认一次；
- **记忆溯源 UI**：`source_message_ids` / `prompt_version` / `extract_model` 已经记了，可以暴露成"这条记忆从哪句话来"的追溯接口。

### E. 可观测性（当前几乎为零）

- `trace_id` 贯穿 `chat → agent → tool → kafka → consumer`，日志里带上；
- 指标：LLM token 消耗与成本、工具调用成功率/耗时、outbox 积压深度、reconcile pending 数；
- `/healthz`（依赖探活）+ `/readyz`，配合 Docker healthcheck。

### F. 工程化基建

- pytest + 针对 `_test_*.py` 现有脚本的正式化改造（现在是根目录一次性脚本）；
- GitHub Actions：ruff + mypy + pytest；
- Dockerfile（多阶段）+ docker-compose 里加 healthcheck 与启动顺序依赖（你已经被"Qdrant 晚几秒就绪"坑过一次，值得在编排层根治）；
- Alembic 管理迁移：现在根目录散着 6 个 `_migrate_*.sql` 手工脚本，没有版本管理，迟早出事。

---

## 六、修复优先级

**第一批（今天）**
1. 🔴-2 移除硬编码密钥 + 轮换
2. 🔴-4 补 `.gitignore`
3. 🔴-1 `task_repo` 启动问题
4. 🔴-3 `requirements.txt` 转 UTF-8 + 精简

**第二批（本周）**
5. 🟠-5 重复 `message.start`
6. 🟠-6 consumer stop 分支
7. 🟠-7/+8 连接池幂等化 + 收敛（这两个一起做）
8. 🟠-12 lifespan 补关闭（对你热重载开发体感最明显）

**第三批（下个迭代）**
9. 🟠-9 code_runner 加固
10. 🟠-10 SQL 注释/语义修正
11. 🟠-11 任务重启恢复
12. 🟠-13 非流式超时
13. 🟡 其余 + 扩展点 B/C/E

---

## 附：评审中确认"做得好"的地方

- **slot 收敛设计**（`profile_service.py` 顶部注释 + `retrieval_service._converge_by_slot`）：识破"按 memory_type 建键会互相覆盖"这个坑并给出按 slot 建键的方案，是这套记忆系统正确性的关键，注释里还留了错误案例。
- **降级设计一致**：记忆检索失败 → `[]`，profile 读失败 → `{}`，Redis 读失败 → 无上下文，全链路"增强功能绝不阻断对话"。
- **Outbox Pattern**：Kafka 挂了 Chat 照常返回，relay 自愈 + 直写 PG 兜底，分层干净。
- **DLQ + 异常分类**（`consumer.py`）：可重试 vs 不可重试分得清楚，还有"deserializer 永不 raise"这种踩过坑才知道的细节。
- **prompt 版本号**（`_EXTRACTION_PROMPT_VERSION`）：记忆抽取结果可追溯是哪版 prompt 生成的，长期维护价值很高。
