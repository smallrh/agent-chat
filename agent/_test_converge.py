import asyncio
from app.service import retrieval_service as rs
from app.service.retrieval_service import _converge_by_slot
from app.core.config import settings


def test_converge():
    raw = [
        {"memory_id": "m1", "slot": "primary_language", "content": "Java", "created_at": "2026-01-01T00:00:00", "score": 0.9},
        {"memory_id": "m2", "slot": "primary_language", "content": "Go", "created_at": "2026-03-01T00:00:00", "score": 0.7},
        {"memory_id": "m3", "slot": "residence_city", "content": "北京", "created_at": "2026-01-02T00:00:00", "score": 0.85},
        {"memory_id": "m4", "slot": "residence_city", "content": "深圳", "created_at": "2026-03-02T00:00:00", "score": 0.6},
        {"memory_id": "m5", "slot": "os", "content": "Windows", "created_at": "2026-01-03T00:00:00", "score": 0.8},
        {"memory_id": "m6", "slot": "os", "content": "macOS", "created_at": "2026-03-03T00:00:00", "score": 0.5},
        {"memory_id": "m7", "slot": None, "content": "火锅", "created_at": "2026-02-01T00:00:00", "score": 0.75},
    ]
    conv = _converge_by_slot(raw)
    contents = [m["content"] for m in conv]
    print("CONVERGED=", contents)
    assert "Go" in contents and "Java" not in contents
    assert "深圳" in contents and "北京" not in contents
    assert "macOS" in contents and "Windows" not in contents
    assert "火锅" in contents  # 无 slot 独立保留
    print("SLOT_CONVERGE_OK dropped=", len(raw) - len(conv))


async def test_retrieve_converge():
    async def fake_search(**kw):
        return [
            {"memory_id": "m1", "score": 0.9, "payload": {}},
            {"memory_id": "m2", "score": 0.7, "payload": {}},
            {"memory_id": "m3", "score": 0.85, "payload": {}},
            {"memory_id": "m4", "score": 0.6, "payload": {}},
        ]

    async def fake_get(ids, user_id=None):
        table = {
            "m1": {"memory_id": "m1", "memory_type": "preference", "slot": "primary_language", "content": "Java", "importance": 0.8, "confidence": 0.9, "created_at": "2026-01-01T00:00:00", "metadata": {}},
            "m2": {"memory_id": "m2", "memory_type": "preference", "slot": "primary_language", "content": "Go", "importance": 0.8, "confidence": 0.9, "created_at": "2026-03-01T00:00:00", "metadata": {}},
            "m3": {"memory_id": "m3", "memory_type": "location", "slot": "residence_city", "content": "北京", "importance": 0.8, "confidence": 0.9, "created_at": "2026-01-02T00:00:00", "metadata": {}},
            "m4": {"memory_id": "m4", "memory_type": "location", "slot": "residence_city", "content": "深圳", "importance": 0.8, "confidence": 0.9, "created_at": "2026-03-02T00:00:00", "metadata": {}},
        }
        return {k: v for k, v in table.items() if k in ids}

    svc = rs.RetrievalService()
    svc._started = True
    svc.embed = lambda t: __import__("asyncio").sleep(0, result=[0.1,0.2,0.3])
    svc.qdrant_repo.search = fake_search
    svc.memory_repo.get_by_ids = fake_get
    svc.memory_repo.record_access = lambda ids: None

    # 开关开启：收敛到 2 条（Go / 深圳）
    settings.MEMORY_CONVERGE_BY_SLOT = True
    res_on = await svc.retrieve(1, "我住哪", record_access=False)
    got_on = [r["content"] for r in res_on]
    print("RETRIEVE_ON=", got_on)
    assert "Go" in got_on and "Java" not in got_on
    assert "深圳" in got_on and "北京" not in got_on
    assert len(res_on) == 2

    # 开关关闭：不过滤，4 条全保留
    settings.MEMORY_CONVERGE_BY_SLOT = False
    res_off = await svc.retrieve(1, "我住哪", record_access=False)
    assert len(res_off) == 4, len(res_off)
    settings.MEMORY_CONVERGE_BY_SLOT = True
    print("TOGGLE_OK")


if __name__ == "__main__":
    test_converge()
    asyncio.run(test_retrieve_converge())
    print("ALL_OK")
