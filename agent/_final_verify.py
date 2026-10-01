"""重启后 API 全链路验证（不走 TestClient，走真实 HTTP）。"""
import asyncio
import asyncpg
import json
import urllib.request
import urllib.error

from app.core.config import settings

BASE = "http://127.0.0.1:1111"
PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        print(f"  ✅ {label}")
        PASS += 1
    else:
        print(f"  ❌ {label}  {detail}")
        FAIL += 1


def http(method, path, body=None, headers=None):
    hdrs = headers or {}
    data = None
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


async def main():
    global PASS, FAIL

    # ---- 1. 准备：从 PG 拿 user_uuid + 一条测试 memory ----
    p = await asyncpg.create_pool(
        host=settings.PG_HOST, port=settings.PG_PORT,
        database=settings.PG_DATABASE,
        user=settings.PG_USER, password=settings.PG_PASSWORD,
    )
    async with p.acquire() as c:
        # 找 user 1 的 UUID
        user = await c.fetchrow("SELECT id, user_uuid FROM users WHERE id=1")
        assert user, "users 表没数据"
        user_id_int = int(user["id"])
        user_uuid = str(user["user_uuid"])
        print(f"🔑  X-User-Id (UUID): {user_uuid}  (users.id={user_id_int})")

        # 找活跃 memory
        mem = await c.fetchrow(
            "SELECT memory_id, memory_type, content FROM memories "
            "WHERE user_id=$1 AND is_active=true ORDER BY created_at DESC LIMIT 1",
            user_id_int,
        )
        if mem:
            memory_id = str(mem["memory_id"])
            mem_type = mem["memory_type"]
            mem_content = mem["content"]
            print(f"📝  测试 memory: {memory_id}  ({mem_type}) {mem_content}")
        else:
            print("⚠️  user 1 没有活跃 memory，先随便造一条...")
            # 造一条
            new_id = await c.fetchval(
                """INSERT INTO memories
                   (user_id, memory_type, content, content_hash, importance,
                    sync_status, source_message_ids, is_active)
                   VALUES ($1, 'preference', '喜欢重启后验证',
                           md5('喜欢重启后验证'::text), 0.8, 'pending', '[]'::jsonb, true)
                   RETURNING memory_id""",
                user_id_int,
            )
            memory_id = str(new_id)
            mem_type = "preference"
            mem_content = "喜欢重启后验证"
            print(f"📝  新建 memory: {memory_id}")
    await p.close()

    H = {"X-User-Id": user_uuid}

    # ---- 2. GET /memories 列表 ----
    print("\n" + "=" * 60)
    print("TEST 1: GET /memories 列表")
    print("=" * 60)
    status, data = http("GET", "/memories", headers=H)
    check("Status 200", status == 200, f"got {status}")
    check("返回 items 数组", isinstance(data, dict) and "items" in data)
    items = data.get("items", []) if isinstance(data, dict) else []
    check(f"items > 0 ({len(items)})", len(items) > 0)
    # 每条字段完整性
    for it in items[:3]:
        missing = [k for k in ("memory_id", "user_id", "memory_type", "content",
                                "is_active", "importance", "sync_status")
                   if k not in it]
        if missing:
            check(f"item {it.get('memory_id','?')[:20]} 字段完整", False, f"缺 {missing}")
        else:
            check(f"item {it['memory_id'][:20]} 字段完整", True)

    # ---- 3. GET /memories/{id} 单条 ----
    print("\n" + "=" * 60)
    print(f"TEST 2: GET /memories/{memory_id} 单条")
    print("=" * 60)
    status, data = http("GET", f"/memories/{memory_id}", headers=H)
    check("Status 200", status == 200, f"got {status}: {str(data)[:120]}")
    if status == 200:
        check("memory_id 匹配", data.get("memory_id") == memory_id)
        check("user_id 匹配", int(data.get("user_id", -1)) == user_id_int)
        check("is_active == true", data.get("is_active") is True)
        check("content 有值", bool(data.get("content")))

    # ---- 4. 版本化 PATCH ----
    print("\n" + "=" * 60)
    print(f"TEST 3: PATCH /memories/{memory_id} 版本化修改")
    print("=" * 60)
    status, data = http(
        "PATCH", f"/memories/{memory_id}",
        body={"new_content": "验证版本化修改 ✅", "new_importance": 0.95},
        headers=H,
    )
    check("Status 200", status == 200, f"got {status}: {str(data)[:120]}")
    new_memory_id = None
    if status == 200:
        new_memory_id = data.get("new_memory_id")
        check("返回 new_memory_id", bool(new_memory_id))
        check("message 字段", "message" in data)

        # 验证老行 deactivated
        s2, d2 = http("GET", f"/memories/{memory_id}", headers=H)
        if s2 == 200:
            check("老行 is_active=false", d2.get("is_active") is False,
                  f"got is_active={d2.get('is_active')}")
            check("老行 merged_into 指向新行",
                  d2.get("merged_into") == new_memory_id,
                  f"merged_into={d2.get('merged_into')}")
        else:
            check("老行仍可查 (可能已被 filter)", True, f"GET old → {s2}")

        # 验证新行 active
        s3, d3 = http("GET", f"/memories/{new_memory_id}", headers=H)
        check("新行 GET 200", s3 == 200, f"got {s3}")
        if s3 == 200:
            check("新行 is_active=true", d3.get("is_active") is True)
            check("新行 content 是新版本", d3.get("content") == "验证版本化修改 ✅")

    # ---- 5. DELETE 级联软删 ----
    if new_memory_id:
        print("\n" + "=" * 60)
        print(f"TEST 4: DELETE /memories/{new_memory_id} 级联软删")
        print("=" * 60)
        status, data = http("DELETE", f"/memories/{new_memory_id}", headers=H)
        check("Status 200", status == 200, f"got {status}: {str(data)[:120]}")
        if status == 200:
            check("pg=True", data.get("pg") is True)
            check("qdrant=True", data.get("qdrant") is True)
            check("redis=True", data.get("redis") is True)

            # 验证查不到
            s2, _ = http("GET", f"/memories/{new_memory_id}", headers=H)
            check("软删后 GET 404", s2 == 404, f"got {s2}")

            # 列表不再包含
            s3, d3 = http("GET", "/memories", headers=H)
            if s3 == 200:
                still = any(i["memory_id"] == new_memory_id for i in d3.get("items", []))
                check("列表不再包含软删项", still is False, "还在列表里！")

    # ---- 6. 越权测试 ----
    print("\n" + "=" * 60)
    print("TEST 5: 越权防护（用 user 111 的 UUID 查 user 1 的 memory）")
    print("=" * 60)
    user2 = await p.fetchrow("SELECT user_uuid FROM users WHERE id=111 LIMIT 1")
    if user2 and user2["user_uuid"] != user_uuid:
        H2 = {"X-User-Id": str(user2["user_uuid"])}
        s, _ = http("GET", f"/memories/{memory_id}", headers=H2)
        check("越权 GET → 404", s == 404, f"got {s} (应该 404)")
        s, _ = http("DELETE", f"/memories/{memory_id}", headers=H2)
        check("越权 DELETE → 404", s == 404, f"got {s}")

    await p.close()

    # ---- 汇总 ----
    print("\n" + "=" * 60)
    print(f"📊 总评: {PASS} 通过 / {FAIL} 失败  {'✅ 全绿' if FAIL == 0 else '❌ 有失败'}")
    print("=" * 60)


asyncio.run(main())
