# Python Agent 服务规范

## 0. 适用范围

本文件规范 `agent/` 下的 Python Agent 服务开发。

FastAPI + asyncpg + Redis + Qdrant + Kafka 技术栈。

---

## 1. 目录结构

```
agent/
├── app/
│   ├── main.py                # FastAPI 入口 + lifespan
│   ├── core/
│   │   └── config.py          # Pydantic Settings
│   ├── req/                   # 请求 Pydantic 模型
│   │   └── chat_req.py
│   ├── rep/                   # 响应 Pydantic 模型
│   │   └── chat_rep.py
│   ├── api/                   # FastAPI Router
│   │   └── chat.py
│   ├── service/               # 业务层
│   │   ├── chat_service.py
│   │   ├── agent_service.py
│   │   ├── memory_service.py
│   │   ├── message_service.py
│   │   ├── profile_service.py
│   │   └── summary_service.py
│   ├── repo/                  # 数据层（asyncpg / redis / qdrant）
│   │   ├── memory_repo.py
│   │   ├── message_repo.py
│   │   ├── qdrant_repo.py
│   │   └── redis_repo.py
│   ├── kafka/                 # Kafka 异步层
│   │   ├── consumer.py
│   │   ├── memory_worker.py
│   │   ├── memory_trigger.py
│   │   ├── memory_worker_consumer.py
│   │   ├── events.py
│   │   └── producer.py
│   ├── agent/                 # Agent 核心
│   │   ├── agent.py
│   │   ├── llm.py
│   │   └── tool_executor.py
│   ├── tool/                  # Tool 注册
│   │   ├── registry.py
│   │   └── weather.py
│   └── utils/                 # 工具
│       └── sse.py
├── public.sql                 # 初始建表基准（禁止覆盖，用版本化增量）
├── _migrate_memories_indexes.sql  # 已有版本化 SQL 示例
└── requirements.txt
```

**禁止**：新代码放到 `app/` 之外。

---

## 2. 配置管理

使用 Pydantic Settings（`app/core/config.py`）。

所有配置从 **环境变量** 或 `.env` 文件读取，**禁止硬编码**。

配置分类：

| 前缀 | 用途 |
|------|------|
| `PG_*` | PostgreSQL |
| `REDIS_*` | Redis |
| `QDRANT_*` | Qdrant |
| `KAFKA_*` | Kafka |
| `OPENAI_*` | LLM |
| `EMBEDDING_*` | Embedding 模型 |

---

## 3. FastAPI 规范

### Pydantic 模型

请求模型在 `req/`，响应模型在 `rep/`。

禁止在 Router 函数里定义内联 Pydantic 模型。

### Header 依赖注入

用户身份从 `X-User-Id` Header 读，**禁止**从 Body/Query/Cookie 取。

```python
from fastapi import Header, Depends

async def get_user_uuid(
    x_user_id: str = Header(..., alias="X-User-Id"),
) -> str:
    """Go Auth 注入的 user_uuid（UUID string）。"""
    return x_user_id
```

### Context

长耗时操作（LLM 调用、向量搜索、数据库 IO）全部 async。

禁止：

- 同步 Redis 客户端
- 同步 PostgreSQL 客户端（用 asyncpg）
- 在 async 函数里做同步阻塞 IO

---

## 4. asyncpg 规范

### 连接池

统一在 Repository 里管理连接池：

```python
class MemoryRepository:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def start(self):
        self.pool = await asyncpg.create_pool(...)

    async def stop(self):
        if self.pool:
            await self.pool.close()
```

### 类型转换

asyncpg 写入 PostgreSQL 时，必须显式转换 JSON 字符串和 UUID：

- `asyncpg.UUID` —— 不要直接传字符串给 uuid 列
- `json.dumps()` —— jsonb 列需要 JSON 字符串
- `datetime` —— timestamptz 列需要 datetime 对象

参考 [message_repo.py](file:///d:/work/agent_system/agent/app/repo/message_repo.py) 里的 `_to_uuid / _to_int / _to_datetime / _to_jsonb` 工具函数。

### 参数化 SQL

PostgreSQL 参数占位符：`$1, $2, ...`，禁止字符串拼接 SQL。

### Transaction

需要原子性的多表操作用 `async with conn.transaction()`：

```python
async with self.pool.acquire() as conn:
    async with conn.transaction():
        await conn.execute("INSERT INTO ...")
        await conn.execute("INSERT INTO ...")
```

---

## 5. Qdrant 规范

### 设计原则

Qdrant 只做**向量检索索引**，不存事实数据。

- Qdrant `point.id` = PostgreSQL `memories.memory_id`（UUID string）
- Qdrant `payload` = 检索过滤用的少量元数据（`user_id`, `memory_type`, `importance`）
- Memory 完整内容**永远只在 PostgreSQL 里**

### 降级策略

Qdrant 连不上时，MemoryService 应该 gracefully skip Qdrant 写入/检索，**不能让整个 Chat / Memory 功能挂掉**。

参考 [qdrant_repo.py](file:///d:/work/agent_system/agent/app/repo/qdrant_repo.py) 的 `start()` 降级模式。

---

## 6. Redis 规范

### 用途

Redis 主要用于：

- 会话历史缓存（`app/repo/redis_repo.py`）
- 对话 summary 缓存

PostgreSQL 才是消息和会话的权威存储，Redis 是缓存层，**缓存 miss 不能阻断功能**。

### Key 命名

使用统一前缀：

```
conv:{conversation_id}:summary
conv:{conversation_id}:messages
```

### TTL

会话缓存必须设置 TTL，避免 Redis 爆内存。

配置项：`REDIS_CONV_TTL`（已在 config.py 中定义）。

---

## 7. Kafka 规范

### 角色

- **Producer**（同步写）：ChatService 在消息落库时发 MemoryEvent
- **Message Consumer**：消费 `KAFKA_MEMORY_TOPIC`，写入 PostgreSQL（messages + conversations）
- **Memory Worker Consumer**：消费 `KAFKA_MEMORY_TASK_TOPIC`，执行 LLM extract + Memory 写入 + Qdrant upsert

### Event 结构（MemoryEvent）

| 字段 | 类型 | 说明 |
|------|------|------|
| `event_id` | UUID string | 事件唯一 ID |
| `event_type` | string | `"message.created"` 等 |
| `user_id` | **int** | `users.id`（Python 内部自增 id，不是 UUID） |
| `conversation_id` | UUID string | |
| `message_id` | UUID string | |
| `role` | string | `"user"` / `"assistant"` |
| `content` | string | |
| `timestamp` | ISO 8601 string | |
| `metadata` | dict | 可选 |

### 分区策略

用 `conversation_id` 作为 Kafka message key，保证同一会话的消息进入同一个 partition，**保证顺序性**。

### 幂等

Consumer 落库时必须保证幂等（Kafka 至少一次投递，可能重放）：

- `messages.message_id` 有 UNIQUE 约束 → `ON CONFLICT DO NOTHING`
- `memories (user_id, memory_type, content)` 完全相同则跳过

### Lifespan 顺序

```
1. Kafka Producer  ← 消息写 Kafka 的前提
2. Message Consumer ← 消息落库
3. Memory Worker Consumer（隔离异常，失败不阻塞主服务）
```

参考 [main.py](file:///d:/work/agent_system/agent/app/main.py) 的 lifespan 实现。

---

## 8. SSE 流式规范

### 格式

使用 `event:` + `data:` 格式，每条事件以空行结束。

参考 [utils/sse.py](file:///d:/work/agent_system/agent/app/utils/sse.py) 的 `sse()` 工具函数：

```python
yield sse("message.delta", {"message_id": "...", "content": "你好"})
```

输出：

```
event: message.delta
data: {"message_id":"...","content":"你好","timestamp":"2026-09-23T18:20:00.650Z"}

```

### 事件顺序

```
agent.start → message.start → (message.delta × N) → message.end → agent.end → done
                                 ↑ tool.start → tool.result → tool.end 可穿插在 delta 之间
```

### 必须发送 done

**无论成功还是失败**，都必须发送 `done` 事件。前端以 `done` 为准结束 loading。

Error 事件之后也要补 `done`：

```python
yield sse("error", {...})
yield sse("done", {...})  # 必须有
```

### 消息落库时机

流式响应的消息**在 SSE 尾部事件之前落库**（参考 chat_service.stream_chat 实现）。保证前端收到 `done` 时消息已经持久化。

---

## 9. 日志规范

使用标准库 `logging`（uvicorn 自带），**禁止引入 structlog / loguru** 等替代。

```python
logger = logging.getLogger(__name__)
```

Logger 名字与模块路径对应：`app.service.chat_service`。

### 禁止敏感日志

绝对禁止日志输出：

- 用户消息原文（可能含密码、token）—— 只打长度或前 50 字符
- Authorization Header
- 完整 JWT
- LLM API Key

参考 [qdrant_repo.py](file:///d:/work/agent_system/agent/app/repo/qdrant_repo.py) 里 `content=%.50s` 的做法。

---

## 10. Graceful Shutdown

`lifespan` 的 `yield` 之后必须：

1. cancel 所有 asyncio.Task
2. stop Kafka producer + consumers
3. close asyncpg pool
4. close Redis
5. close Qdrant client

**顺序**：先停入口（Producer），再停消费端（Consumer），最后停数据库连接。

参考 [main.py](file:///d:/work/agent_system/agent/app/main.py) 的 shutdown 实现。

---

## 11. 错误处理

### Logger 层级

- `logger.exception()` —— 捕获异常时打（自动包含 traceback）
- `logger.error()` —— 预期失败但不需要 traceback
- `logger.warning()` —— 降级/非致命问题
- `logger.info()` —— 关键业务节点

### 降级优先

外部依赖（Qdrant、Redis、Kafka）失败时，优先降级，不要让主流程崩掉：

- Qdrant 挂了 → 跳过向量索引
- Redis 挂了 → 无上下文对话，直接调 Agent
- Kafka 挂了 → Chat API 仍可响应，但消息不落库

### 禁止 panic

Python 里是**禁止抛异常到顶层**？不，这里明确一下：

- **API 入口**可以抛 HTTPException（FastAPI 会处理）
- **Service / Repo 层**应该让异常自然向上传播，FastAPI 入口统一处理
- **Kafka Consumer** 内部的循环异常要捕获，不能让整个 Consumer 崩掉
- **lifespan 启动阶段**的硬依赖（Producer）失败应该 raise，让服务不启动

---

## 12. SQL 文件版本化

同 [03-数据库规范.md](file:///d:/work/agent_system/backend/.agent/rules/03-数据库规范.md) 第 12 节。

格式：`v<yyyyMMdd>_<序号>_<描述>.sql`

例如现有的 `_migrate_memories_indexes.sql` 应该重命名为：

```
v20260926_01_create_memories_indexes.sql
```

并加上标准变更头。

---

## 13. 跨服务身份

### 从 Header 读 user_uuid（UUID）

Python Agent 从 `X-User-Id` Header 读 user_uuid（UUID string）。

### 内部需要 users.id（int8）

conversations.user_id / messages.user_id / memories.user_id 都是 FK 指向 `users.id`（int8）。

所以 Python Agent 入口需要做转换：`user_uuid → users.id`。

详见 [12-跨服务契约规范.md](file:///d:/work/agent_system/backend/.agent/rules/12-跨服务契约规范.md) 第 2 节。
