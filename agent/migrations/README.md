# 迁移文件

由 `_migrate_*.sql` / `public*.sql` 收编而来，用 `python -m app.repo.migrator` 管理。

## 命名

`V{三位版本号}__{描述}.sql`，按文件名排序执行。

## 现有环境怎么办（重要）

这些脚本**在当前库里大概率已经执行过了**。首次接入时要先 stamp，不能直接 apply：

```bash
# 1) 先看计划（不会执行任何 SQL）
.venv/Scripts/python.exe -m app.repo.migrator plan

# 2) 确认都已手动执行过 → 标记为已应用
.venv/Scripts/python.exe -m app.repo.migrator stamp V009__tasks.sql

# 3) 之后再新增迁移，就可以正常 apply 了
.venv/Scripts/python.exe -m app.repo.migrator apply
```

## 新增迁移

1. 在 migrations/ 下新建 `V010__xxx.sql`
2. 保证可重复执行安全（用 `IF NOT EXISTS` / `DO $$ ... $$` 判断）
3. `python -m app.repo.migrator apply`
