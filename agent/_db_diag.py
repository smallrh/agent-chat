import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect(
        host="127.0.0.1", port=5432,
        database="agent_system", user="postgres", password="your_password",
    )
    try:
        # 1) users table
        try:
            users = await conn.fetch(
                "SELECT id, user_uuid FROM users ORDER BY id"
            )
            print("== USERS ==")
            for u in users:
                print(dict(u))
        except Exception as e:
            print("USERS table error:", repr(e))

        # 2) target user
        print("\n== TARGET USER (e6a818ec...) ==")
        row = await conn.fetchrow(
            "SELECT id, user_uuid FROM users WHERE user_uuid=$1",
            "e6a818ec-d586-42ee-ad5d-d7594a282578",
        )
        print(dict(row) if row else "NOT FOUND")
        target_id = row["id"] if row else None

        # 3) memories overview
        try:
            total = await conn.fetchval("SELECT count(*) FROM memories")
            print("\n== MEMORIES total rows ==")
            print("total:", total)
            per_user = await conn.fetch(
                "SELECT user_id, count(*) AS c, "
                "count(*) FILTER (WHERE is_active) AS active, "
                "count(*) FILTER (WHERE sync_status='pending') AS pending, "
                "count(*) FILTER (WHERE sync_status='indexed') AS indexed "
                "FROM memories GROUP BY user_id ORDER BY user_id"
            )
            print("== MEMORIES per user_id ==")
            for r in per_user:
                print(dict(r))
        except Exception as e:
            print("MEMORIES overview error:", repr(e))

        # 4) memories for target user
        if target_id is not None:
            rows = await conn.fetch(
                "SELECT memory_id, memory_type, content, sync_status, "
                "is_active, sync_retry_count, retry_after, created_at "
                "FROM memories WHERE user_id=$1 ORDER BY created_at DESC",
                target_id,
            )
            print(f"\n== MEMORIES for user_id={target_id} ({len(rows)} rows) ==")
            for r in rows:
                print({k: (str(v)[:80] if v is not None else None) for k, v in dict(r).items()})

        # 5) the 8 pending rows (reconcile count)
        pend = await conn.fetch(
            "SELECT memory_id, user_id, memory_type, content, sync_status, "
            "sync_retry_count, retry_after FROM memories "
            "WHERE sync_status='pending' ORDER BY user_id, created_at"
        )
        print(f"\n== PENDING rows ({len(pend)}) ==")
        for r in pend:
            print({k: (str(v)[:60] if v is not None else None) for k, v in dict(r).items()})

    finally:
        await conn.close()

asyncio.run(main())
