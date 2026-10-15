
## 推荐方案（用户说"给我推荐"——要的是判断不是选项清单）

产出 `recommendations-2026-10-09.md`，并把结论写进工程档案 ADR-013/014/015（状态=拟议）。

### C 中期 → 推荐：PG 租约 + Redis Pub/Sub，不引入 Kafka topic
- 任务分发：`tasks` 加 `owner` + `lease_expires_at`，`SELECT ... FOR UPDATE SKIP LOCKED` 抢租约，心跳续租 60s。
- 进度广播：`TaskEventBus` 换 Redis Pub/Sub（事件瞬时、丢得起，不需要 Kafka 持久化）。
- 不推荐 Kafka 分发的理由：要额外带来 topic/consumer/幂等/offset + "建行但消息没发出"的一致性问题，
  而当前真实需求只有"重启不丢任务"（recover_interrupted 已解决）+"将来多副本"，PG 租约 50 行 SQL 就够。
- 升级触发条件：单进程吞吐跟不上 / 单任务分钟级 / 需跨语言消费。

### D 遗忘 → 推荐：只做检索侧降权，不删不归档
- `score_final = cosine × 0.5^(days_since_access / half_life)`，half_life 按 importance 分级
  （>=0.8→180d，0.5~0.8→60d，<0.5→30d），**必须用现有 memories 数据回测校准**（变化率>20% 说明太激进）。
- 只降权不删除的理由：降权改参数可回退、删除不可逆；配合已有的 score_threshold=0.35，
  降到阈值下的记忆自然不注入，效果等同遗忘但数据完整。
- 归档触发条件：单用户 >5000 条 或 检索 p95 明确因数据量上升。

### D 冲突确认 → 推荐：静默收敛 + 事后可追溯，不做弹窗
- 保持静默保留最新；旧值用 `merged_into`/`is_active=false` 留着；加 `GET /memories/{id}/history`。
- 只在同 slot 30 天内变更 ≥3 次时，回答末尾附带一句轻量确认（不打断）。
- 不做弹窗的理由：记忆是后台异步抽取的，抽取发生在对话之后，根本没有合适的弹窗时机。

### 执行顺序（别跳序）
1 修 4 个🔴 → 2 修 4 个已知 bug（连接池/lifespan/message.start/consumer stop）→ 3 tasks 租约
→ 4 遗忘降权 → 5 记忆 history → 6 取消粒度/code_runner → 7（默认不做）Kafka 分发/真归档/冲突弹窗。
明确建议先别做：Alembic、trace 写进 Kafka payload、LLM 成本看板、/metrics 接 Prometheus。
