from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_HOST: str
    APP_PORT: int
    RELOAD: bool

    OPENAI_API_KEY: str
    OPENAI_BASE_URL: str
    OPENAI_MODEL: str
    OPENAI_STREAM: bool
    # 多步工具循环（ReAct）最大轮次：防止模型反复调用工具停不下来
    MAX_TOOL_ROUNDS: int = 10

    # LLM 调用超时（秒）：客户端级兜底 + 每次请求级超时。
    # 非流式 /chat 没有 cancel token，模型卡住会长期占住 worker 线程和连接池，
    # 这个超时是目前唯一的兜底手段。
    LLM_TIMEOUT_SECONDS: float = 120.0

    # ---------------------------------------------------------
    # 工具层（app/tool/）：自动发现 + 执行护栏
    # ---------------------------------------------------------
    # 关闭后注册表为空（用于排障：逐个排除工具干扰）
    TOOL_DISCOVERY_ENABLED :bool = True

    # 默认单次调用超时（秒）。工具可以用 TOOL_CONFIG 里的 timeout 覆盖。
    # 注意：同步工具跑在线程里，超时只是"调用方不等了"，线程不会被杀，
    # 因此工具自身仍必须内置资源上限（如 code_runner 的 subprocess timeout）。
    TOOL_DEFAULT_TIMEOUT :float = 20.0

    # 失败重试次数（不含首次）；0 = 不重试
    TOOL_MAX_RETRIES :int = 1
    # 指数退避基数（秒）
    TOOL_RETRY_BACKOFF :float = 0.5

    # 熔断：连续失败多少次后开闸 + 冷却秒数
    TOOL_CIRCUIT_THRESHOLD :int = 5
    TOOL_CIRCUIT_RESET_SECONDS :float = 60.0

    # 按 user_id 限流：单个工具每分钟最多调用几次；0 = 不限。
    # 工具可用 TOOL_CONFIG 的 quota_per_minute 覆盖（例如联网搜索配小一点）。
    TOOL_DEFAULT_QUOTA_PER_MINUTE :int = 60

    # 同步工具执行线程池大小
    TOOL_EXECUTOR_WORKERS :int = 8

    # 本轮有多个独立工具调用时并发执行（无依赖的工具，如同时查天气和当前时间）。
    # 注意：并发不改变 messages 里的顺序，结果仍按 tool_calls 原始顺序回填。
    TOOL_PARALLEL_ENABLED :bool = True
    # 单轮最多并发多少个工具（超出则退回串行，避免一次把线程池打满）
    TOOL_MAX_PARALLEL :int = 4

    # 审计日志是否落参数内容（含用户隐私，默认只记参数名与长度）
    TOOL_AUDIT_LOG_ARGS :bool = False

    # -------- 可观测性 --------
    # 是否开启 /metrics 端点（关掉后返回 404）
    METRICS_ENABLED :bool = True
    # 访问 /metrics 需要的请求头 X-Metrics-Token；留空表示不校验
    # （生产建议配上，或在 Go 网关层直接禁掉 /metrics /readyz 的外部访问）
    METRICS_TOKEN :str = ""

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

    # 联网搜索（web_search 工具用）
    # 默认提供方 exa：走 Exa OpenAI 兼容 Responses API（model="agent"），
    # 需配置 EXA_API_KEY；未单独配置时回退到 WEB_SEARCH_API_KEY。
    # 设为 tavily 可切换回 Tavily（用 WEB_SEARCH_API_KEY）。
    # 注意：这两个 Key 属于密钥，**不要**在代码里写默认值，必须从 .env 注入。
    # （历史遗留：WEB_SEARCH_API_KEY 曾硬编码真实 Key，已迁移到 agent/.env）
    EXA_API_KEY: str = ""
    WEB_SEARCH_API_KEY: str = ""
    WEB_SEARCH_PROVIDER: str = "exa"

    # ---------------------------------------------------------
    # 事实 grounding（P0：引用强制 + 禁止无结果编造）
    # ---------------------------------------------------------
    # 总开关：关闭后回退到优化前的行为（只靠提示词约束）。
    GROUNDING_ENABLED: bool = True
    # 工具结果为空/失败时，往模型上下文里注入「禁止编造」提醒
    GROUNDING_INJECT_REMINDER: bool = True
    # 最终答案里出现「工具原文中不存在的 URL」时是否直接剔除。
    # 默认 False：只记告警日志，不动正文（避免误删模型自己拼的合法链接）。
    GROUNDING_STRIP_UNKNOWN_URLS: bool = False
    # 检索明确失败/无结果时，是否在答案末尾追加「未能核实」的降级说明
    GROUNDING_APPEND_FAILURE_NOTE: bool = True

    # ---------------------------------------------------------
    # 搜索去重（P1：相似 query 不二次搜索）
    # ---------------------------------------------------------
    # 除「完全相同的 query」外，是否也复用「高度相似的 query」的结果。
    # 典型场景：模型把「广州天河酒店」改成「广州天河区酒店推荐」再搜一遍。
    SEARCH_DEDUP_SIMILAR_ENABLED: bool = True
    # 相似度阈值（0~1，交集 / 较小集合的 token 数）。0.72 为经验值：
    # 既能吃掉「换个近义词」，又不会让「广州酒店」吃掉「北京酒店」。
    SEARCH_DEDUP_SIMILARITY: float = 0.72
    # 单轮对话（一次用户提问）内 web_search 最多调用几次。
    # 0 = 不限。设 2 表示「允许一次失败后改写关键词重试」，堵住反复搜索。
    SEARCH_MAX_CALLS_PER_TURN: int = 2

    # ---------------------------------------------------------
    # 后台任务队列（P2：真 worker）
    # ---------------------------------------------------------
    # 单用户同时在跑（pending/running）的任务上限。
    # 后台任务各自占 LLM + 检索配额，不设上限时「连点 10 次建任务」
    # 会直接把 Agent 的线程池和搜索预算打满，连正常对话都变慢。
    TASK_MAX_ACTIVE_PER_USER: int = 3
    # 全局同时在跑的任务上限（单实例部署下的兜底）
    TASK_MAX_ACTIVE_GLOBAL: int = 20
    # research worker：最多拆几个检索词
    TASK_RESEARCH_MAX_QUERIES: int = 3
    # research worker：每个检索词取多少条结果
    TASK_RESEARCH_TOP_K: int = 5
    # research worker：单次 LLM 调用超时（秒）（规划 / 汇总各一次）
    TASK_RESEARCH_LLM_TIMEOUT: float = 120.0
    # research worker：注入给汇总 LLM 的资料原文长度上限（字符）
    TASK_RESEARCH_MAX_EVIDENCE_CHARS: int = 12000

    # ---------------------------------------------------------
    # 附件 / 多模态（P2）
    # ---------------------------------------------------------
    # 附件落盘目录（相对 agent/ 或绝对路径均可）
    ATTACHMENT_DIR: str = "./data/attachments"
    # 单个附件大小上限（字节），默认 10MB
    ATTACHMENT_MAX_BYTES: int = 10 * 1024 * 1024
    # 单条消息最多带几个附件
    ATTACHMENT_MAX_PER_MESSAGE: int = 5
    # 注入给 LLM 的单个附件文本长度上限（字符），超出截断
    ATTACHMENT_MAX_TEXT_CHARS: int = 8000
    # 是否把图片以 base64 多模态内容块发给模型。
    # 关闭时图片退化成「（图片：xxx.png）」占位文本注入 ——
    # 用于模型不支持 vision 时避免 400。
    ATTACHMENT_IMAGE_ENABLED: bool = True
    # 允许上传的 MIME 白名单前缀（逗号分隔；空 = 不限制）
    ATTACHMENT_ALLOWED_MIME_PREFIXES: str = (
        "image/,text/,application/pdf,application/json,"
        "application/vnd.openxmlformats-officedocument,"
        "application/vnd.ms-excel,application/msword"
    )

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
