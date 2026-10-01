"""
一次性清理脚本（运维用）：
  A) 删除 user_id=1 的两条占位记忆「已更新的记忆内容（测试版本化）」
     - PG: hard_delete（真正删行）
     - Qdrant: hard_delete 对应点（memory_id 即 point id）
  B) 清掉被删占位行留下的 dangling merged_into（指向这两条的幸存真实记忆）
  C) 删除 Qdrant 中 user_id ∈ {123, 99998} 的孤儿点（PG 已无对应行）

用法：
  python _cleanup_memories.py            # 仅打印将要做什么（DRY RUN）
  python _cleanup_memories.py --commit   # 真正执行删除
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv(".env")
sys.path.insert(0, os.getcwd())

from app.repo.memory_repo import MemoryRepository
from app.repo.qdrant_repo import QdrantRepository
from qdrant_client import models

USER_UUID = "e6a818ec-d586-42ee-ad5d-d7594a282578"
PLACEHOLDER = "已更新的记忆内容（测试版本化）"
ORPHAN_USER_IDS = [123, 99998]


async def main():
    commit = "--commit" in sys.argv
    mem = MemoryRepository()
    qd = QdrantRepository()
    await mem.start()
    await qd.start()

    try:
        uid = await mem.pool.fetchval(
            "SELECT id FROM users WHERE user_uuid=$1", USER_UUID
        )
        print(f"[PG] target user: user_uuid={USER_UUID} -> id={uid}")

        # ---- A) 占位行 ----
        ph_rows = await mem.pool.fetch(
            "SELECT memory_id, content, memory_type, is_active, merged_into "
            "FROM memories WHERE user_id=$1 AND content=$2 ORDER BY memory_type",
            uid, PLACEHOLDER,
        )
        print(f"\n[A] placeholder rows for user {uid}: {len(ph_rows)}")
        ph_ids = [str(r["memory_id"]) for r in ph_rows]
        for r in ph_rows:
            print(f"   {r['memory_id']} type={r['memory_type']} "
                  f"active={r['is_active']} merged_into={r['merged_into']}")

        # ---- B) 幸存真实记忆（merged_into 指向占位行）----
        survivors = []
        if ph_ids:
            survivors = await mem.pool.fetch(
                "SELECT memory_id, content, merged_into FROM memories "
                "WHERE user_id=$1 AND merged_into = ANY($2::uuid[])",
                uid, ph_ids,
            )
        print(f"\n[B] survivors with merged_into -> placeholder: {len(survivors)}")
        for s in survivors:
            print(f"   {s['memory_id']} '{s['content']}' -> merged_into={s['merged_into']}")

        # ---- C) Qdrant 孤儿点 ----
        orphans = {}
        if qd.client:
            for oid in ORPHAN_USER_IDS:
                try:
                    pts, _ = await qd.client.scroll(
                        collection_name=qd._collection,
                        scroll_filter=models.Filter(must=[
                            models.FieldCondition(
                                key="user_id",
                                match=models.MatchValue(value=oid),
                            )
                        ]),
                        limit=200,
                        with_payload=True,
                        with_vectors=False,
                    )
                    orphans[oid] = [str(p.id) for p in pts]
                    print(f"\n[C] Qdrant orphan points user_id={oid}: {len(orphans[oid])} {orphans[oid]}")
                except Exception as e:
                    print(f"\n[C] scroll user_id={oid} FAILED: {type(e).__name__}: {e}")
        else:
            print("\n[C] Qdrant client not connected, skip orphan scan")

        if not commit:
            print("\n*** DRY RUN — pass --commit to actually delete ***")
            return

        # ================= COMMIT =================
        del_pg = 0
        for r in ph_rows:
            mid = str(r["memory_id"])
            ok = await mem.hard_delete(mid)
            del_pg += 1 if ok else 0
            await qd.hard_delete(mid)
        print(f"\n[COMMIT A] hard-deleted {del_pg}/{len(ph_rows)} placeholder rows from PG + Qdrant points")

        if survivors:
            sids = [s["memory_id"] for s in survivors]
            await mem.pool.execute(
                "UPDATE memories SET merged_into=NULL, updated_at=now() "
                "WHERE memory_id = ANY($1::uuid[])",
                sids,
            )
            print(f"[COMMIT B] cleared merged_into on {len(survivors)} survivor rows: "
                  f"{[str(s) for s in sids]}")

        for oid, ids in orphans.items():
            if ids and qd.client:
                await qd.client.delete(
                    collection_name=qd._collection,
                    points_selector=models.PointIdsList(points=ids),
                )
                print(f"[COMMIT C] deleted {len(ids)} Qdrant orphan points for user_id={oid}")
    finally:
        await mem.stop()
        await qd.stop()


if __name__ == "__main__":
    asyncio.run(main())
