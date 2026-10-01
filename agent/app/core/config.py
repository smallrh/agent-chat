from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_HOST: str
    APP_PORT: int
    RELOAD: bool

    OPENAI_API_KEY: str
    OPENAI_BASE_URL: str
    OPENAI_MODEL: str
    OPENAI_STREAM: bool

    # Embedding（LM Studio 本地 nomic-embed-text，OpenAI 兼容）
    # 注意：OpenAI SDK 会在 base_url 后面自动拼 /embeddings，
    # 所以这里必须是 ".../v1"，不能是 ".../v1/embeddings"（否则变成双重 /embeddings/embeddings → 空响应）。
    EMBEDDING_BASE_URL: str = "http://127.0.0.1:1234/v1"
    EMBEDDING_API_KEY: str = "text-embedding-nomic-embed-text-v1.5"   # LM Studio 不校验 key，占位即可
    EMBEDDING_MODEL: str = "text-embedding-nomic-embed-text-v1.5"
    MAX_TOKENS: int
    TEMPERATURE:float

    GEOCODING_URL:str
    WEATHER_URL:str

    # =========================================================
    # 持久化存储配置（Docker 容器）
    # =========================================================

    # ---------------------------------------------------------
    # PostgreSQL
    # ---------------------------------------------------------
    PG_HOST :str
    PG_PORT :int
    PG_DATABASE :str
    PG_USER :str
    PG_PASSWORD :str
    PG_POOL_SIZE :int

    # ---------------------------------------------------------
    # Redis
    # ---------------------------------------------------------
    REDIS_HOST :str
    REDIS_PORT :int
    REDIS_PASSWORD :str
    REDIS_DB :int
    REDIS_POOL_SIZE :int
    REDIS_KEY_PREFIX :str
    # 会话历史 Redis 缓存过期时间（秒）
    REDIS_CONV_TTL :int

    # ---------------------------------------------------------
    # Qdrant
    # ---------------------------------------------------------
    QDRANT_HOST :str
    QDRANT_PORT :int
    QDRANT_GRPC_PORT :int
    QDRANT_COLLECTION_KNOWLEDGE :str
    QDRANT_COLLECTION_LTM :str
    QDRANT_COLLECTION_AGENT :str
    # Embedding 向量维度（bge-small-zh）
    QDRANT_VECTOR_SIZE :int

    # ---------------------------------------------------------
    # Qdrant 连接韧性（启动重试 + 后台探活重连）
    # ---------------------------------------------------------
    # 启动期连接重试次数/间隔：兼容 docker-compose 启动顺序
    # （Qdrant 容器可能比 app 晚几秒就绪，单点连接会误报"连不上"）
    QDRANT_STARTUP_MAX_RETRIES :int = 3
    QDRANT_STARTUP_RETRY_INTERVAL :float = 1.5
    # 后台探活/重连周期（秒）：解决「Qdrant 中途重启 / 启动期一直没连上」
    # 导致连接永久卡在 None 的问题——健康循环会持续尝试直到连上，
    # 不必等某个业务操作（upsert/search）来触发。
    QDRANT_HEALTH_INTERVAL_SECONDS :int = 15

    # ---------------------------------------------------------
    # 检索（读取端）默认参数
    # ---------------------------------------------------------
    # 语义检索 cosine 下限（低于此分的命中直接丢弃）。
    # nomic-embed-text-v1.5 中文区分度一般，0.35 是兼顾召回与精度的经验值。
    MEMORY_SEARCH_SCORE_THRESHOLD :float = 0.35
    # 单次检索默认返回条数上限
    MEMORY_SEARCH_DEFAULT_TOP_K   :int   = 10

    # ---------------------------------------------------------
    # 记忆注入 Agent 上下文的预算
    # ---------------------------------------------------------
    # 单次注入最多携带多少条记忆（先按 score 降序，超出截断）
    MEMORY_INJECT_MAX_ITEMS :int = 10
    # 注入记忆占用的 token 预算上限（中英文混合粗略估算，超出停止追加）
    MEMORY_INJECT_TOKEN_BUDGET :int = 1200

    # ---------------------------------------------------------
    # 冲突收敛（同槽位多版本只注入最新）
    # ---------------------------------------------------------
    # 是否按 slot（属性槽位）收敛：同一 slot 只保留 created_at 最新的一条，
    # 旧版本 / 矛盾记忆不再注入。False=关闭（每条记忆独立注入，旧行为）。
    MEMORY_CONVERGE_BY_SLOT :bool = True

    # ---------------------------------------------------------
    # MinIO
    # ---------------------------------------------------------
    MINIO_ENDPOINT :str
    MINIO_ACCESS_KEY :str
    MINIO_SECRET_KEY :str
    MINIO_BUCKET :str
    MINIO_SECURE :bool

    KAFKA_HOST :str
    KAFKA_PORT :int
    KAFKA_BOOTSTRAP_SERVERS :str

    # Message event topic（Chat API 写入，Message Consumer 消费）
    KAFKA_MEMORY_TOPIC :str
    KAFKA_CONSUMER_GROUP :str

    # DLQ topic（Consumer 3 次 retry 仍失败的脏消息 / 致命异常）
    KAFKA_MEMORY_DLQ_TOPIC :str = "memory.events.dlq"

    # Consumer 消费失败重试配置
    CONSUMER_MAX_RETRIES :int = 3
    CONSUMER_RETRY_BASE_DELAY :float = 1.0       # 指数退避基数（秒）
    CONSUMER_RETRY_MAX_DELAY :float = 8.0        # 退避封顶（秒）
    CONSUMER_RETRY_JITTER :float = 0.5           # 随机抖动（秒）

    # Memory task topic（Message Consumer 写入，Memory Worker 消费）
    KAFKA_MEMORY_TASK_TOPIC :str
    KAFKA_MEMORY_WORKER_GROUP :str

    # Memory reconcile topic（对账任务触发，扫 pending 行重跑 embed+Qdrant）
    KAFKA_MEMORY_RECONCILE_TOPIC :str = "memory.reconcile"
    KAFKA_MEMORY_RECONCILE_GROUP :str = "memory-reconcile-group"
    # 每次对账最多扫多少条 pending
    RECONCILE_BATCH_SIZE :int = 100
    # 对账批次间 sleep（秒），控 embedding/Qdrant 速率
    RECONCILE_ITEM_INTERVAL :float = 1.0
    # 周期性对账：除「启动时发一次」外，每隔多少秒再发一次 reconcile 触发。
    # 默认 300s（5 分钟）。解决「reconcile 仅启动时触发一次」导致 LM Studio
    # 抖动脉冲期错过后、pending 记忆长期积压的问题——下一周期会自动补跑。
    # reconcile_once 本身对单条失败走指数退避（mark_pending_failed + retry_after），
    # 空闲时直接 return，开销极小。
    RECONCILE_INTERVAL_SECONDS :int = 300

    # ---------------------------------------------------------
    # 近似合并
    # ---------------------------------------------------------
    # 两阶段：向量粗筛 + LLM 精判
    #   1) Qdrant find_similar_in_type 按 MEMORY_MERGE_VECTOR_THRESHOLD 粗筛候选
    #      （nomic-embed-text-v1.5 对中文区分度差 → 0.60 就够覆盖近义词）
    #   2) 候选送 LLM 二元判断"是否同一事实"，YES 才 merge
    #
    # MEMORY_MERGE_LLM_JUDGE 开关：True=两阶段（推荐），False=仅向量粗筛
    #
    # 经验教训：
    #   nomic-embed-text-v1.5 把"讨厌吃辣"vs"喜欢吃辣"cos=1.0（否定词忽略）、
    #   "喜欢吃辣"vs"来自四川"cos=0.69（跨主题也高分）→ 纯向量不可靠，必须 LLM 精判
    MEMORY_MERGE_VECTOR_THRESHOLD :float = 0.60
    MEMORY_MERGE_LLM_JUDGE        :bool  = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()