# 项目长期记忆 (agent_system/agent)
## 配置了热更新，不要启动我的代码，我的代码是一直启动着的
## PG 连接池（2026-10-09 重构，务必遵守）
- **全进程只有一个 PG 连接池**：`app/repo/pg.py` 提供 `get_pool()` / `close_pool()` / `current_pool()`。
  `MemoryRepository / MessageRepository / TaskRepository / OutboxRepository / core.auth` 全部取共享池
  （此前 9 个独立池 → PG 连接数 9×PG_POOL_SIZE，热重载再翻倍）。
- 新增任何仓储都不要 `asyncpg.create_pool()`，一律 `self.pool = await get_pool()`。
- 仓储的 `start()` 必须幂等；**`stop()` 只解引用不关池**（否则一个组件关闭会掐掉别人的连接），
  真正的关闭只在 `main.py` lifespan shutdown 里做一次。
- `ConversationRepository` 委托 `MessageRepository` 的池，自己不建池。

## 安全约定（2026-10-09 起生效）
- **密钥类配置一律空默认**，真实值只能经 `agent/.env` 注入（`.env` 已被根 `.gitignore` 忽略，勿跟踪）。
  违例会 Timeline：曾把真实 Tavily key 写成 `WEB_SEARCH_API_KEY` 默认值，差点提交到
  git@github.com:smallrh/agent-chat.git（已改回 `""` 并按泄露待轮换）。
- 密钥一旦进过任何**被跟踪或可能被提交**的文件（含 .md 评审稿），即按已泄露 → 立即轮换。
- 轮换后记得同步 `.env`：`.env` 当前**没有** WEB_SEARCH_API_KEY / EXA_API_KEY，
  缺失时 `web_search` 会降级为「未配置 Key」文案（不抛异常、不阻断对话）。

## 运行约定
- **启动 app 一律用项目 `.venv`**，不要用 conda `py3147`：
  `D:/work/agent_system/agent/.venv/Scripts/python.exe -m app.main`
  （曾因双实例 + 解释器不一致导致 Qdrant 连接假死、配置/依赖漂移。）
- 依赖环境：PG 5432 / Redis 6379 / Kafka 9092 / Qdrant 6333(REST，未开 gRPC 6334) / LM Studio 1234。
  Qdrant `AsyncQdrantClient` 必须 `prefer_grpc=False`（容器只暴露 6333）。

## 工具安全开关（2026-10-09）
- `ENABLE_CODE_RUNNER` 默认 **false**；`CODE_RUNNER_MAX_TIMEOUT`=10（LLM 传入会被钳制）；
  `LLM_TIMEOUT_SECONDS`=120（llm.py 客户端级 + 每次请求超时）。改动前先确认默认值没被放宽。

## 任务自愈与非流式取消（2026-10-09）
- `TaskService.start()` 会扫 `pending/running` 旧行置 failed（error="interrupted by restart"）并发事件；
  该逻辑假设单实例，多副本前需加 owner/lease 过滤。
- 非流式 `/chat` 也返回 `run_id`（`ChatResponse`），`Agent.chat` 支持 `cancel_token` 协作式取消
  （每轮开始 + 每个工具前检查）。

## 记忆子系统设计要点
- PG `memories` 是权威存储；Qdrant `long_term_memory` 仅做语义检索索引（id=memory_id）。
- embedding: LM Studio `text-embedding-nomic-embed-text-v1.5`，dim=768。
  `EMBEDDING_BASE_URL` 必须是 `http://127.0.0.1:1234/v1`（OpenAI SDK 会自动拼 `/embeddings`，写 `.../v1/embeddings` 会双重拼接导致空响应）。
- reconcile 触发：启动时 1 次 + 每 `RECONCILE_INTERVAL_SECONDS`(=300) 周期 1 次（Kafka topic `memory.reconcile`）。
- Qdrant 连接韧性：`get_qdrant_repo()` 单例 + `start()` 启动重试(3×1.5s) + `_qdrant_health_loop` 每 15s 探活重连。

## 用户/测试数据
- user_uuid `e6a818ec-d586-42ee-ad5d-d7594a282578` → users.id=1；其真实记忆：`用户的名字是 Alice`(name)、`用户喜欢吃火锅`(preference)。

## 开发协作约定
- **工具实现分工（已变更）**：原本约定"工具逻辑用户自己实现"，但用户在 2026-10-09 后续明确要求
  **由助手直接实现业务工具**（search_memory / web_search / calculator / code_runner 等均已落地）。
  故：框架/循环/执行器/注册表/schema 由助手搭，工具函数体也由助手写；用户保留审阅与替换权。

## P2 多任务架构（tasks 表 / 进度 / 后台写回）
- 表 `tasks`（见 `public.tasks.sql`，需手动执行到 PG）：task_id(uuid) / conversation_id / user_id /
  title / goal / status(pending→running→succeeded|failed|cancelled) / progress(0-100) / result(jsonb) / error。
- 分层：`app/repo/task_repo.py`（asyncpg，照搬 message_repo 约定）、`app/service/task_events.py`
  （进程内 pub/sub：conversation_id→set(asyncio.Queue)，供 `/tasks/stream` SSE）、
  `app/service/task_service.py`（`TaskService` 单例：spawn_task 建行+`asyncio.create_task(_run)`；
  _run 逐步 progress 入库+广播，成功经 `message_service.create_assistant_message` 后台写回会话）。
- 端点 `app/api/tasks.py`：`POST /tasks`、`GET /tasks`、`GET /tasks/{id}`、`POST /tasks/{id}/cancel`、
  `GET /tasks/stream`(SSE)。**Go BFF 需透明透传 `/tasks/stream` SSE（同 /chat/stream）**。
- Agent 工具 `create_task`（已注册）可在对话中拉起后台任务；`agent.py chat/stream` 已透传
  `conversation_id` 给 `tool_executor`。
- 扩展真实业务：在 `TaskService` 用 `register_handler("kind", handler)` 注册，
  handler 签名 `async def (task_id, progress, conversation_id, user_id, goal, title)`。
- **注意**：热更新环境我不直连 DB；建表 SQL 需用户手动执行。
- **仓储实例唯一性（血泪坑，勿回退）**：`TaskRepository` **没有**模块级单例（原 `task_repo`
  从未 start()，导致 `/tasks` 全部端点 500，已于 2026-10-09 删除）。
  <｜hy_place▁holder▁no▁813｜> tasks 表统一走 `TaskService.repo`（`app/api/tasks.py` 里封装成 `_repo()`，
  未就绪抛 503）。**新增任何 tasks 相关代码都不要 `from app.repo.task_repo import task_repo`**。
- 工具执行约束（实现工具时务必注意）：`tool_executor.execute` 跑在 `asyncio.to_thread`（worker 线程）。
  纯同步/HTTP(httpx) 工具直接写即可；**任何需要主事件循环单例（Qdrant/PG/async 检索）的工具**不能跨 loop 复用单例，
  需改用「per-loop 客户端」或「async 工具在原 loop await」方案。
  search_memory 采用 **run_coroutine_threadsafe 桥接主循环** 复用 `memory_retrieval_service` 单例（见 tool_executor）。

## 联网搜索（web_search 工具）
- 默认提供方 **Exa**：OpenAI 兼容 Responses API（base_url="https://api.exa.ai"、model="agent"、流式、
  json_schema 结构化 answer+sources）。配置 `EXA_API_KEY`（未配置回退 `WEB_SEARCH_API_KEY`）。
- `WEB_SEARCH_PROVIDER` 默认 `exa`；设为 `tavily` 切回 Tavily 兜底（用 `WEB_SEARCH_API_KEY`）。
- 无 key 时返回清晰提示、不抛异常；函数签名 `web_search(query, top_k=5) -> str`（LLM 契约不变）。

## 跨会话长期记忆（用户基础信息如何"新会话也记得"）
- 架构本就跨会话：`memories` 按 user_id 存（conversation_id 可 NULL，唯一键按 user 去重）；
  `retrieval_service.retrieve` 只按 user_id+is_active 过滤，**不按 conversation 限制**。
- 双通道注入（二者互补，缺一不可）：
  1. **语义检索**（按需召回，新会话闲聊开场可能召回不到）→ `chat_service._retrieve_memories`
  2. **Profile 常驻块**（无条件每轮注入）→ `chat_service._load_profile` → `agent._format_profile_block`
     Redis `user:{id}:profile`，由 `ProfileService.update` 在抽取后写入；读不到时从 PG 重建并回填。
- **profile 键必须按 `slot`，不能按 memory_type**：memory_type 太粗（籍贯和现居地都属 location），
  按它建键会互相覆盖。籍贯=`hometown`、现居地=`residence_city` 按 slot 并存；偏好按
  favorite_food/favorite_sport 分开。同 slot 多版本按时间取最新。
  改键语义要 bump `PROFILE_SCHEMA_VERSION`（当前 2），旧缓存会自动按新规则从 PG 重建。
  抽取 prompt 改 slot 语义时必须 bump `_EXTRACTION_PROMPT_VERSION`（当前 v2）。
- 抽取触发 `app/kafka/memory_trigger.py`：周期(sequence_id%10) + **early_threshold=1（会话首条必抽）**
  + 信号正则（已含籍贯/身份：我是X人 / 来自 / 老家在 / 住在）。置 0 可关闭首条触发。
- 坑：`agent.chat/stream` 的 `profile` 形参插在 `memories` 之后 → **必须关键字传参**，位置传参会错位。
- 坑：`app/service/agent_service.py` 的同名 `ChatService` 是**死代码**；实时链路是
  `app/api/chat.py → app/service/chat_service.py`，改记忆注入要改后者。
