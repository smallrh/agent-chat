"""工程化基建（一）：
1. 把根目录散落的 _migrate_*.sql / public*.sql 收编进 migrations/，带版本号
   （**复制而非移动**，原文件保留，不打断现有工作流）
2. requirements.txt 从 UTF-16 LE 转 UTF-8（pip 现在读不了这个文件）
3. docker-compose.yml 给 Kafka 补 healthcheck（其它依赖早已有）
"""

import io
import os
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS = os.path.join(ROOT, "migrations")

# 版本号 → 源文件。顺序按语义依赖排列：
# 先有表（public），再有 memories 的演进，最后 tasks。
LEGACY_SQL = [
    ("V001__public_baseline.sql", "public.sql"),
    ("V002__outbox_events.sql", "_migrate_outbox_events.sql"),
    ("V003__memories_content_hash.sql", "_migrate_memories_content_hash.sql"),
    ("V004__memories_sync_status.sql", "_migrate_memories_sync_status.sql"),
    ("V005__memories_origin.sql", "_migrate_memories_origin.sql"),
    ("V006__memories_indexes.sql", "_migrate_memories_indexes.sql"),
    ("V007__memories_crud.sql", "_migrate_memories_crud.sql"),
    ("V008__memories_merge.sql", "_migrate_memories_merge.sql"),
    ("V009__tasks.sql", "public.tasks.sql"),
]


def import_legacy():
    os.makedirs(MIGRATIONS, exist_ok=True)
    for target, source in LEGACY_SQL:
        src = os.path.join(ROOT, source)
        if not os.path.exists(src):
            print(f"[skip] source missing: {source}")
            continue
        dst = os.path.join(MIGRATIONS, target)
        shutil.copyfile(src, dst)
        print(f"[ok] {source} -> migrations/{target}")

    readme = os.path.join(MIGRATIONS, "README.md")
    io.open(readme, "w", encoding="utf-8").write(
        """# 迁移文件

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
"""
    )
    print("[ok] migrations/README.md")


def fix_requirements():
    path = os.path.join(ROOT, "requirements.txt")
    raw = io.open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
        io.open(path, "w", encoding="utf-8").write(text)
        print("[ok] requirements.txt UTF-16 -> UTF-8")
    else:
        print("[skip] requirements.txt already UTF-8")


def fix_compose():
    path = os.path.join(ROOT, "agent-infra", "docker-compose.yml")
    s = io.open(path, encoding="utf-8").read()
    if "kafka-health" in s or "KAFKA_HEALTH" in s:
        print("[skip] kafka healthcheck exists")
        return

    anchor = """    volumes:
      - kafka_data:/var/lib/kafka/data

    networks:
      - agent-net
"""
    new = """    volumes:
      - kafka_data:/var/lib/kafka/data

    # Kafka 没有官方 healthcheck 命令，用自带的工具探测 broker 可用性。
    # 注意：KRaft 单节点下 topic 自动创建被关掉了（KAFKA_AUTO_CREATE_TOPICS_ENABLE=false），
    # 所以 app 启动前要确保 topic 已建好，否则 producer 会一直等 metadata。
    healthcheck:
      test:
        - CMD-SHELL
        - /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list || exit 1
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 30s

    networks:
      - agent-net
"""
    if anchor not in s:
        print("[skip] kafka volume anchor not found")
        return
    s = s.replace(anchor, new)
    io.open(path, "w", encoding="utf-8").write(s)
    print("[ok] docker-compose.yml + kafka healthcheck")


if __name__ == "__main__":
    import_legacy()
    fix_requirements()
    fix_compose()
