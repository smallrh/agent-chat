"""极简 SQL 迁移器（零新依赖）。

## 为什么不用 Alembic

Alembic 的核心价值之一是 **autogenerate**：拿 SQLAlchemy 的 metadata 和数据库
对比，自动生成迁移脚本。但本项目全程裸 SQL + asyncpg，**没有 ORM 模型**，
autogenerate 用不上；为了它引入 `alembic` + `sqlalchemy[asyncio]` 两个依赖，
换来的是"手写 SQL + 版本表"——而这个用 100 行就能做，且与现有技术栈一致。

如果将来接了 ORM，再换 Alembic 也不迟：迁移文件是纯 SQL，可以直接搬过去。

## 用法（从项目根目录执行）

    .venv/Scripts/python.exe -m app.repo.migrator plan      # 只看计划，不执行
    .venv/Scripts/python.exe -m app.repo.migrator apply     # 执行待应用的迁移
    .venv/Scripts/python.exe -m app.repo.migrator stamp V009__tasks.sql
                                                            # 标到某版本（不执行）
    .venv/Scripts/python.exe -m app.repo.migrator history   # 已应用的版本

## 首次接入（重要）

现有库里这些脚本**已经手动执行过了**，直接 apply 会报错（重复加列/建索引）。
正确姿势是先 stamp 到当前最新版本，见 migrations/README.md。

## 事务

每个迁移文件在**单个事务**里执行：文件内部失败会整体回滚，不会留下半成品
（DDL 在 PostgreSQL 里也是事务性的，这点很重要）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from app.core.config import settings
from app.repo.pg import close_pool, get_pool

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "migrations",
)

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


# ---------------------------------------------------------------------------
# 发现迁移文件
# ---------------------------------------------------------------------------


def discover() -> list[tuple[str, str]]:
    """返回 [(version, path)]，按版本号排序。"""
    if not os.path.isdir(MIGRATIONS_DIR):
        return []
    files = [
        name for name in os.listdir(MIGRATIONS_DIR)
        if name.endswith(".sql")
    ]
    return [(name, os.path.join(MIGRATIONS_DIR, name)) for name in sorted(files)]


async def applied_versions(pool) -> set[str]:
    await pool.execute(_BOOTSTRAP_SQL)
    rows = await pool.fetch("SELECT version FROM schema_migrations")
    return {row["version"] for row in rows}


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


async def plan(pool) -> list[tuple[str, str]]:
    applied = await applied_versions(pool)
    pending = [(v, p) for v, p in discover() if v not in applied]
    return pending


async def apply(pool) -> int:
    """执行所有待应用的迁移，返回本次应用的数量。"""
    pending = await plan(pool)
    if not pending:
        print("没有待应用的迁移（已是最新）")
        return 0

    for version, path in pending:
        sql = _read(path)
        # 空文件（只有注释）直接标记完成，避免 asyncpg 报空语句
        if not _has_statement(sql):
            await pool.execute(
                "INSERT INTO schema_migrations(version) VALUES($1) "
                "ON CONFLICT (version) DO NOTHING",
                version,
            )
            print(f"  [skip-empty] {version}")
            continue

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES($1) "
                "ON CONFLICT (version) DO NOTHING",
                version,
            )
        print(f"  [applied] {version}")

    print(f"完成：应用了 {len(pending)} 个迁移")
    return len(pending)


async def stamp(pool, version: str) -> None:
    """把某个版本（及其之前的版本）标记为已应用，不执行 SQL。"""
    all_versions = [v for v, _ in discover()]
    if version not in all_versions:
        raise SystemExit(
            f"未知版本：{version}；可选：{', '.join(all_versions) or '(空)'}"
        )
    idx = all_versions.index(version)
    for v in all_versions[: idx + 1]:
        await pool.execute(
            "INSERT INTO schema_migrations(version) VALUES($1) "
            "ON CONFLICT (version) DO NOTHING",
            v,
        )
    print(f"已标记 {idx + 1} 个版本为已应用（最新：{version}）")


async def history(pool) -> None:
    rows = await pool.fetch(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    )
    if not rows:
        print("尚未应用任何迁移")
        return
    for row in rows:
        print(f"  {row['version']}  {row['applied_at']}")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _has_statement(sql: str) -> bool:
    """去掉注释和空行后还有内容才算有语句。"""
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, force=True)

    if not argv:
        print(__doc__)
        return 1

    cmd, *rest = argv
    pool = await get_pool()
    try:
        if cmd == "plan":
            pending = await plan(pool)
            applied = await applied_versions(pool)
            print(f"已应用 {len(applied)} 个，待应用 {len(pending)} 个")
            for version, _ in pending:
                print(f"  [pending] {version}")
        elif cmd == "apply":
            await apply(pool)
        elif cmd == "stamp":
            if not rest:
                raise SystemExit("用法：stamp <version>")
            await stamp(pool, rest[0])
        elif cmd == "history":
            await history(pool)
        else:
            raise SystemExit(f"未知命令：{cmd}（可选：plan / apply / stamp / history）")
    finally:
        # CLI 是自己起的进程，跑完要主动关池（常驻服务里不要这么做）
        await close_pool()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main(sys.argv[1:])))
    except SystemExit:
        raise
    except Exception as exc:                              # noqa: BLE001
        print(f"迁移失败：{type(exc).__name__}: {exc}")
        print(f"（连接参数：{settings.PG_HOST}:{settings.PG_PORT}/{settings.PG_DATABASE}）")
        sys.exit(2)
