# 项目长期记忆 (agent_system/agent)

## 运行约定
- **启动 app 一律用项目 `.venv`**，不要用 conda `py3147`：
  `D:/work/agent_system/agent/.venv/Scripts/python.exe -m app.main`
  （曾因双实例 + 解释器不一致导致 Qdrant 连接假死、配置/依赖漂移。）
- 依赖环境：PG 5432 / Redis 6379 / Kafka 9092 / Qdrant 6333(REST，未开 gRPC 6334) / LM Studio 1234。
  Qdrant `AsyncQdrantClient` 必须 `prefer_grpc=False`（容器只暴露 6333）。

## 记忆子系统设计要点
- PG `memories` 是权威存储；Qdrant `long_term_memory` 仅做语义检索索引（id=memory_id）。
- embedding: LM Studio `text-embedding-nomic-embed-text-v1.5`，dim=768。
  `EMBEDDING_BASE_URL` 必须是 `http://127.0.0.1:1234/v1`（OpenAI SDK 会自动拼 `/embeddings`，写 `.../v1/embeddings` 会双重拼接导致空响应）。
- reconcile 触发：启动时 1 次 + 每 `RECONCILE_INTERVAL_SECONDS`(=300) 周期 1 次（Kafka topic `memory.reconcile`）。
- Qdrant 连接韧性：`get_qdrant_repo()` 单例 + `start()` 启动重试(3×1.5s) + `_qdrant_health_loop` 每 15s 探活重连。

## 用户/测试数据
- user_uuid `e6a818ec-d586-42ee-ad5d-d7594a282578` → users.id=1；其真实记忆：`用户的名字是 Alice`(name)、`用户喜欢吃火锅`(preference)。
