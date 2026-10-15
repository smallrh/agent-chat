import re
import logging

logger = logging.getLogger(__name__)


# 可能触发 Memory 抽取的关键词 / 句式
_MEMORY_SIGNAL_PATTERNS: list[re.Pattern] = [
    # 明确的身份 / 职业陈述（支持 "我是一个 Java 开发工程师" / "我是开发" / "我是名程序员" 等）
    re.compile(r"我是(?:一个|名|位)?\s*\S*\s*(?:工程师|开发|程序员|产品经理|设计师|老师|医生|律师|学生|研究员|架构师|经理|总监|主管)", re.I),
    re.compile(r"我(?:的|们)(?:名字|生日|电话|地址|工作|公司|学校|家乡|职业|专业)", re.I),
    re.compile(r"(?:住在|居住|老家|在.*工作|在.*上学|就职于|任职于)"),
    re.compile(r"(?:叫|名字是|我是.*岁|我叫)"),
    # 偏好 / 习惯
    re.compile(r"我(?:喜欢|讨厌|爱吃|不吃|最爱|常用|总是|一般|通常|习惯)", re.I),
    re.compile(r"(?:偏好|不喜欢|最喜欢)"),
    # 籍贯 / 所在地 / 身份归属（"我是四川人"/"我来自成都"/"我老家在湖南"/"我住在北京"）
    # 注：以"人"结尾并要求后接句末或标点/空白，避免把"我是好人"这类口语过度泛化
    re.compile(
        r"我是(?:一个|一名|名|位|个)?\s*\S{1,12}?人(?=$|[，。,.！!？?、；;\s])",
        re.I,
    ),
    # 注意："我的籍贯是四川" 中间有个"的"，故需覆盖 的籍贯是/的祖籍 等变体；
    # 长分支要写在短分支前面（正则分支是顺序匹配）。
    re.compile(
        r"我(?:来自|老家在|老家是|的老家在|的老家|老家"
        r"|籍贯是|的籍贯是|的籍贯|籍贯"
        r"|祖籍是|的祖籍是|的祖籍|祖籍"
        r"|户籍是|现居|定居在|居住在|住在)\s*\S+",
        re.I,
    ),
]


class MemoryTrigger:
    """
    判断本轮对话是否需要做 Memory 抽取。

    触发策略（满足任一即可）：
      1. 周期触发：每 N 条消息（例如 10 条）做一次
      2. 早期触发：会话开头几条（用户常在此自报家门），保证短会话也至少抽一次
      3. 信号触发：最近消息里包含身份/偏好/事实类关键词
    """

    def __init__(self, cycle_threshold: int = 10, early_threshold: int = 1):
        self.cycle_threshold = cycle_threshold
        # early_threshold=1 表示"会话第 1 条消息必抽一次"；
        # 置 0 可关闭（避免每个新会话都多一次 LLM 抽取开销）。
        self.early_threshold = early_threshold

    async def should_extract(
        self,
        messages: list[dict],
    ) -> bool:
        """
        判断是否需要做 Memory 抽取。

        - messages: Redis 里拉的最近 N 条（顺序 = 旧 → 新）
        - 周期触发：最新消息的 sequence_id 到达 cycle_threshold 的倍数
          （用 sequence_id 而不是 len(messages)，因为 Redis 只保留最近 20 条，
           len 永远是 20）
        - 信号触发：最新一条消息里含身份/偏好关键词
        """
        if not messages:
            return False

        latest = messages[-1]
        latest_sequence_id = latest.get("sequence_id")
        latest_content = latest.get("content", "")

        # 早期触发：会话开头几条必抽一次。
        # 解决"短会话（<10 条）且未命中任何信号正则 → 事实永远不会被记下"的漏网问题
        # （例如用户在会话 A 第一句说"我是四川人"，之后就不再触发抽取）。
        if (
            self.early_threshold
            and latest_sequence_id is not None
            and 0 < latest_sequence_id <= self.early_threshold
        ):
            logger.info(
                "MemoryTrigger: early hit (sequence_id=%s <= %s)",
                latest_sequence_id, self.early_threshold,
            )
            return True

        # 周期触发：用 sequence_id
        if (
            latest_sequence_id is not None
            and latest_sequence_id > 0
            and latest_sequence_id % self.cycle_threshold == 0
        ):
            logger.info(
                "MemoryTrigger: cycle hit "
                "(sequence_id=%s %% %s == 0)",
                latest_sequence_id, self.cycle_threshold,
            )
            return True

        # 信号触发
        if latest_content and self.contains_memory_signal(latest_content):
            logger.info(
                "MemoryTrigger: signal matched in message %s",
                latest.get("message_id", "?"),
            )
            return True

        return False

    @staticmethod
    def contains_memory_signal(content: str) -> bool:
        if not content:
            return False
        for pat in _MEMORY_SIGNAL_PATTERNS:
            if pat.search(content):
                logger.debug("Memory signal matched: %s", pat.pattern)
                return True
        return False
