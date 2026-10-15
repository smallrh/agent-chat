# 推荐方案：C 中期 / D 记忆 / 后续执行顺序

> 每项都给一个明确推荐，附理由、代价、以及**什么时候该换另一种方案**的触发条件。
> 判断基准：你现在的实际状态是单人开发、本地热重载、单进程、用户量小。
> 在这个阶段，**少一个中间件就是少一类故障**。

---

## 一、C 中期：后台任务如何演进

### 推荐：PG 租约 + Redis Pub/Sub，**不引入新 Kafka topic**

| 环节 | 推荐方案 | 备选（不推荐现在做） |
|---|---|---|
| 任务分发 | PG `FOR UPDATE SKIP LOCKED` 抢租约 | 新增 Kafka topic + 独立 worker 进程 |
| 进度广播 | Redis Pub/Sub | outbox → Kafka → 各副本消费 |
| 重启恢复 | 租约过期自动回收 | （同左，已有 `recover_interrupted`） |

### 为什么不是 Kafka 分发

Kafka 分发要额外带来：1 个 topic、1 个 consumer group、消费幂等、offset 管理、
"任务已建行但消息没发出去"的一致性问题。而你现在的真实需求只是两条：
**重启不丢任务**（`recover_interrupted` 已经解决）和**将来能跑多副本**。

PG 租约用 50 行 SQL 就能同时满足，且 PG 你已经有了、已经在用、已经有监控表。

### 落地骨架

```sql
-- migrations/V010__task_lease.sql
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS owner            text,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_tasks_claim
    ON tasks (status, created_at)
    WHERE status IN ('pending', 'running');
```

```sql
-- 抢任务（每副本一个 owner，周期执行）
UPDATE tasks
   SET owner = $1,
       lease_expires_at = now() + interval '60 seconds',
       status = 'running'
 WHERE task_id IN (
     SELECT task_id FROM tasks
      WHERE status IN ('pending', 'running')
        AND (lease_expires_at IS NULL OR lease_expires_at < now())
      ORDER BY created_at
      FOR UPDATE SKIP LOCKED
      LIMIT $2
 )
 RETURNING task_id;
```

配套三点：
1. **心跳续租**：任务运行中每 20s 把 `lease_expires_at` 推到 `now() + 60s`
2. **回收**：启动时 + 周期扫 `lease_expires_at < now() and status='running'` → 重置为 pending（或按重试次数置 failed）
3. **进度广播**：`TaskEventBus.publish` 改为 `redis.publish(f"agent:task:{conversation_id}", json)`，
   SSE 端点 `subscribe` 对应 channel。事件是瞬时的，丢了不心疼，**不需要持久化到 Kafka**

### 触发升级条件（满足任一再考虑 Kafka）

- 单进程吞吐跟不上（任务排队 > 5 分钟）
- 单任务耗时 > 分钟级，且需要跨服务调用
- 要跨语言消费任务（比如 Go 侧也要领任务）

---

## 二、D 记忆：遗忘曲线

### 推荐：**只做检索侧降权，不做删除/归档**

```
score_final = cosine × decay
decay       = 0.5 ^ (days_since_last_access / half_life)

half_life 按 importance 分级（初始值，需实测校准）：
    importance >= 0.8  →  180 天
    0.5 ~ 0.8          →   60 天
    < 0.5              →   30 天
```

放在 `retrieval_service.retrieve` 里，Qdrant 召回之后、slot 收敛之前。
由 `MEMORY_DECAY_ENABLED` 开关控制，默认**开启但 half_life 先设大**（比如统一 365 天），
观察一段时间再按数据收紧。

### 为什么不做真删除

- **可逆性**：降权改个参数就能回退；删掉的数据找不回来
- **产品价值**：你这系统的卖点是"记住用户"。误删一条"用户是四川人"的体验损失，
  远大于检索慢 20ms 的损失
- **淘汰已经存在**：配合 `MEMORY_SEARCH_SCORE_THRESHOLD`（0.35），降权到阈值以下的
  记忆自然就不会被注入 —— 效果等同于遗忘，但数据是完整的

### half_life 怎么校准（不要拍脑袋）

用现有数据回测，不用等线上：

```sql
SELECT memory_type, slot, importance,
       COUNT(*) AS n,
       AVG(now() - COALESCE(last_accessed_at, created_at)) AS avg_idle
FROM memories WHERE is_active = true GROUP BY 1,2,3 ORDER BY n DESC LIMIT 30;
```

然后拿真实 query 跑一遍：分别设 half_life = 30/60/180/365，看 top-10 结果的变化率。
**变化率 > 20% 就说明参数太激进**，往大调。

### 触发归档条件

- 单用户记忆 > 5000 条，或
- 检索 p95 明显上升且确认是数据量导致

满足后再做冷数据归档（移到 `memories_archive` 表，检索时跳过，用户可在设置里恢复）。

---

## 三、D 记忆：冲突确认

### 推荐：**不打断对话，做"静默收敛 + 事后可追溯"**

保持现在的"同 slot 保留最新"行为不变，只补两件事：

1. **保留被覆盖的旧值**：收敛时不要丢，写进 `merged_into` / `is_active=false`（现有字段够用），
   这样用户问"你之前记的是什么"能答出来
2. **给一个可查入口**：复用刚做的 `GET /memories/{id}/source`，再加一个
   `GET /memories/{id}/history` 列出同 slot 的历史取值

**只在一种情况下做轻量确认**：同一 slot 在 30 天内变更 ≥ 3 次（说明这个属性不稳定），
在回答末尾追加一句「你之前提到过 X，我按最新的 Y 记了，要改吗？」——不打断，只是附带一句。

### 为什么不做弹窗式确认

聊天场景里每记一条事实就弹一次确认，用户三次就烦了。
而且你这套记忆是**后台异步抽取**的（Kafka worker），抽取发生在对话之后，
根本没有"合适的时机"弹确认——强行弹只会让用户觉得莫名其妙。

业界成熟产品（含 ChatGPT memory）也都是静默更新 + 提供查看/删除入口，不是逐条确认。

---

## 四、后续执行顺序推荐

按「价值 ÷ 成本」排，从上往下做。**别跳序**——前一项没做完就做后一项，会返工。

| 序 | 事项 | 为什么排这个位置 |
|---|---|---|
| 1 | 4 个 🔴 阻塞项（密钥轮换、`.gitignore`、`task_repo` 启动、requirements 精简） | 都是"正确性/安全"问题，且互相独立、改动小。里面的 `task_repo` 修完才能验证任务链路 |
| 2 | 连接池 + lifespan 关闭 + 重复 `message.start` + consumer stop 分支 | 这四个都属于"修复已知 bug"，做完系统才真正稳定，后面的改动才有可信的基线 |
| 3 | tasks 租约（本节一） | 依赖第 1 项的 `task_repo` 修复；做完多副本障碍清零 |
| 4 | 遗忘降权（本节二） | 需要第 2 项做完后有可信的检索基线，否则调参没有参照 |
| 5 | 记忆 history 接口 + 静默冲突记录（本节三） | 依赖溯源接口（已交付），增量小 |
| 6 | 并行工具取消粒度优化、`code_runner` 加固 | 收益稳定但不高，排在功能性完成之后 |
| 7 | Kafka 分发 / 真归档 / 冲突弹窗 | **默认不做**，等各自触发条件出现再评估 |

### 明确建议先不要做的

- **Alembic**（除非你接 ORM）：理由见 ADR-012
- **trace_id 写进 Kafka payload**：当前 Kafka 是进程内生产的，跨服务前没收益；等 Go 侧也要接链路追踪时一起做
- **LLM 成本看板**：先拿到模型单价表再说，不要拿估算数字做决策
- **`/metrics` 接 Prometheus**：等真的有 Grafana 需求，进程内 JSON 现在够用

---

## 附：一句话总结

> 先把已知 bug 修干净（1、2），再用 50 行 SQL 把任务做成可多副本（3），
> 记忆只降权不删除（4、5）。Kafka 分发、真归档、冲突弹窗——**都有明确的触发条件，
> 条件没出现就别做**。
