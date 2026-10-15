"""把本轮推荐方案写入工程档案（拟议状态，待用户确认后转生效）。"""

import io
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
p = os.path.join(ROOT, "project-engineering-file.md")
s = io.open(p, encoding="utf-8").read()

anchor = "- 状态：生效（原文件 `_*migrate*.sql` 保留未删，migrations/ 是复制）\n"
assert anchor in s, "ADR-012 anchor missing"

new = anchor + """
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
"""
s = s.replace(anchor, new, 1)

# 待办里补上推荐文档索引
todo_anchor = "## 7. 待办\n"
assert todo_anchor in s
s = s.replace(
    todo_anchor,
    "## 7. 待办\n\n"
    "> 详细推荐方案（含触发条件与执行顺序）见 `recommendations-2026-10-09.md`。\n"
    "> 结论：先修已知 bug → tasks 租约（ADR-013）→ 遗忘降权（ADR-014）→ 记忆 history（ADR-015）；\n"
    "> Kafka 分发 / 真归档 / 冲突弹窗默认不做，等触发条件。\n",
    1,
)

io.open(p, "w", encoding="utf-8").write(s)
print("[ok] project-engineering-file.md + ADR-013/014/015 (拟议)")
