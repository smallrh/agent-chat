
### 2026-10-09：memories 表缺 slot 列 —— 跨会话记忆失效的真正根因

**现象**：日志出现
```
ERROR | app.service.profile_service | ProfileService.retrieve ... UndefinedColumnError: column "slot" does not exist
```
且长期「新会话记不住用户」。

**根因**：`slot` 是演进期新增列。最新 `public.sql` 的 CREATE TABLE 里**有**这一列，
但已有数据库不会自动补列；散落的六个 `_migrate_*.sql` 手工脚本里**没有一个加过它**。

**影响面**（比看起来大）：
- `MemoryRepository.save()` 的 INSERT 引用 `slot` → **每条记忆都写不进 PG**
- `ProfileService.rebuild_from_memories()` 的 SELECT 引用 `slot` → **每次都崩**，降级返回 `{}`
  → profile 块永远为空 → 新会话拿不到用户事实
- 两处都有 `try/except` 吞异常并降级，只在 DEBUG 日志留痕，所以线上看着「没报错」

**修复**：执行 `migrations/V010__memories_slot.sql`（幂等，含列补齐 + 老数据回填 + 索引）。
已执行过的话重复执行无害。

**警示**：`public.sql` 顶部是 `DROP TABLE IF EXISTS ...` + `CREATE TABLE`，
对**已有数据的库是毁灭性的**，只能在初始化全新库时使用。
后续所有 schema 变更一律走 migrations/ 的版本脚本。

**反思**：如果有 migrator（ADR-012），启动时会发现 V010 未应用并自动补上，
不会静默失效。这个 bug 本身就是"缺迁移管理"的代价。
