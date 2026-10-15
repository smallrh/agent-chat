"""
跨会话长期记忆 —— 离线验证（零外部依赖，不连 Redis/PG/Qdrant）。

覆盖：
  #10 MemoryTrigger：籍贯/身份类陈述能被抽取信号命中 + 会话首条消息必抽
  #11 Agent：profile 常驻块无条件注入 system prompt
  #12 ProfileService：Redis 未命中 / schema 升级时从 PG memories 回退重建
  #14 Profile 按 slot 建键：籍贯 vs 现居地并存，不被互相覆盖
"""

import asyncio
import sys

from app.kafka.memory_trigger import MemoryTrigger
from app.service.profile_service import ProfileService


# =========================
# Fake 依赖
# =========================

class FakeRedis:
    def __init__(self, profile=None):
        self._profile = profile
        self.set_calls = []

    async def get_profile(self, user_id):
        return self._profile

    async def set_profile(self, user_id, profile):
        self._profile = profile
        self.set_calls.append(profile)


class FakeMemoryRepo:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.started = False

    async def start(self):
        self.started = True

    async def list_memories(self, user_id, *, active_only=True, limit=50):
        return self.rows


def check(label, cond, detail=""):
    status = "✅" if cond else "❌"
    print(f"{status} {label}" + (f"  {detail}" if detail else ""))
    return cond


async def main():
    ok = True

    # ---------------------------------------------------------
    # #10 抽取触发
    # ---------------------------------------------------------
    print("\n===== #10 MemoryTrigger 抽取触发 =====")
    t = MemoryTrigger()

    for s in ["我是四川人", "我是一个四川人", "我来自成都", "我老家在湖南",
              "我住在北京", "我的籍贯是四川"]:
        ok &= check(f"信号命中: {s}", t.contains_memory_signal(s))

    for s in ["今天天气怎么样", "帮我写个快排"]:
        ok &= check(f"不误触发: {s}", not t.contains_memory_signal(s))

    ok &= check("早期触发: sequence_id=1 必抽",
                await t.should_extract([{"sequence_id": 1, "content": "你好"}]))
    ok &= check("周期触发: sequence_id=10",
                await t.should_extract([{"sequence_id": 10, "content": "哈哈"}]))
    ok &= check("对照: 第3条无信号 → 不抽",
                not await t.should_extract([{"sequence_id": 3, "content": "再想想"}]))

    # ---------------------------------------------------------
    # #14 profile 按 slot 建键（本轮核心修复）
    # ---------------------------------------------------------
    print("\n===== #14 按 slot 建键：籍贯 vs 现居地并存 =====")

    # 同一个 memory_type(location)，两个不同 slot → 必须并存，不能互相覆盖
    mems = [
        {"memory_type": "location", "slot": "residence_city",
         "content": "用户目前在广州上班", "updated_at": "2026-10-01"},
        {"memory_type": "location", "slot": "hometown",
         "content": "用户的籍贯是四川", "updated_at": "2026-10-09"},
    ]
    built = ProfileService.build_profile_from_memories(mems)
    ok &= check("现居地保留(广州)", built.get("residence_city") == "用户目前在广州上班", str(built))
    ok &= check("籍贯保留(四川)", built.get("hometown") == "用户的籍贯是四川", str(built))
    ok &= check("两条并存（未被覆盖）", len(built) == 2, str(built))

    # 多个偏好也按 slot 分开
    prefs = [
        {"memory_type": "preference", "slot": "favorite_food", "content": "用户喜欢吃火锅"},
        {"memory_type": "preference", "slot": "favorite_sport", "content": "用户喜欢打篮球"},
    ]
    p = ProfileService.build_profile_from_memories(prefs)
    ok &= check("偏好按 slot 分开", p.get("favorite_food") == "用户喜欢吃火锅"
                and p.get("favorite_sport") == "用户喜欢打篮球", str(p))

    # 没有 slot → 退回 memory_type 兜底键
    noslot = [{"memory_type": "hobby", "content": "喜欢摄影"}]
    ok &= check("无 slot → 退回 memory_type 键",
                ProfileService.build_profile_from_memories(noslot).get("hobbies") == "喜欢摄影")

    # 同一 slot 多版本 → 最新的胜出（按时间升序，后覆盖前）
    dup = [
        {"memory_type": "location", "slot": "residence_city",
         "content": "用户以前住在北京", "updated_at": "2026-01-01"},
        {"memory_type": "location", "slot": "residence_city",
         "content": "用户现在住在深圳", "updated_at": "2026-09-01"},
    ]
    ok &= check("同 slot 最新版胜出",
                ProfileService.build_profile_from_memories(dup).get("residence_city")
                == "用户现在住在深圳",
                str(ProfileService.build_profile_from_memories(dup)))

    # ---------------------------------------------------------
    # #12 schema 版本迁移 + 回退重建
    # ---------------------------------------------------------
    print("\n===== #12 schema 版本迁移 / 回退 =====")

    V = ProfileService.VERSION_KEY

    # Redis 有当前版本 → 直接返回
    cur = {"residence_city": "广州", V: ProfileService.PROFILE_SCHEMA_VERSION}
    got = await ProfileService(FakeRedis(dict(cur)), FakeMemoryRepo()).get_profile(1)
    ok &= check("Redis 当前版本 → 直接返回", got == cur, str(got))

    # Redis 是旧版（按 memory_type 的扁平键，无版本标记）→ 触发重建
    legacy = {"location": "用户目前在广州上班", "preferences": "用户喜欢吃火锅"}
    fake_redis = FakeRedis(dict(legacy))
    rows = [
        {"memory_type": "location", "slot": "residence_city",
         "content": "用户目前在广州上班", "updated_at": "2026-10-01"},
        {"memory_type": "location", "slot": "hometown",
         "content": "用户的籍贯是四川", "updated_at": "2026-10-09"},
    ]
    got = await ProfileService(fake_redis, FakeMemoryRepo(rows)).get_profile(1)
    ok &= check("旧 schema → 从 PG 重建", got.get("hometown") == "用户的籍贯是四川", str(got))
    ok &= check("重建后旧键不残留", "location" not in got, str(got))
    ok &= check("重建后打上新版本号", got.get(V) == ProfileService.PROFILE_SCHEMA_VERSION)

    # Redis 空 → 重建并回填
    fr = FakeRedis(None)
    got = await ProfileService(fr, FakeMemoryRepo(rows)).get_profile(1)
    ok &= check("Redis 空 → 重建", got.get("residence_city") == "用户目前在广州上班", str(got))
    ok &= check("重建后回填 Redis", len(fr.set_calls) == 1)

    # PG 不可用 → 降级 {}，不抛异常
    class BoomRepo(FakeMemoryRepo):
        async def start(self):
            raise RuntimeError("pg down")
    got = await ProfileService(FakeRedis(None), BoomRepo()).get_profile(1)
    ok &= check("PG 不可用 → 降级 {} (不抛异常)", got == {}, str(got))

    # ---------------------------------------------------------
    # #11 Agent 注入 profile
    # ---------------------------------------------------------
    print("\n===== #11 Agent profile 常驻注入 =====")
    from app.agent.agent import Agent

    agent = Agent()
    profile = {
        "residence_city": "用户目前在广州上班",
        "hometown": "用户的籍贯是四川",
        "_schema_version": 2,
    }

    msgs = agent._build_messages(
        message="我的籍贯是哪里",
        history=[], summary=None,
        memories=None,          # ← 完全无语义召回
        profile=profile,
    )
    system = msgs[0]["content"]
    ok &= check("profile 块已注入", "已知用户基本信息" in system)
    ok &= check("籍贯可见(四川)", "四川" in system)
    ok &= check("现居地可见(广州)", "广州" in system)
    ok &= check("内部字段 _schema_version 不注入", "_schema_version" not in system)
    ok &= check("slot 带中文标签", "籍贯/老家" in system and "现居地/工作地" in system)
    ok &= check("无语义记忆也能看到事实", "用户长期记忆" not in system)

    # 与语义记忆共存
    msgs2 = agent._build_messages(
        message="hi", history=[], summary=None,
        memories=[{"memory_id": "x", "memory_type": "location",
                   "content": "用户来自四川", "confidence": 0.9}],
        profile=profile,
    )
    ok &= check("profile 与语义记忆可共存",
                "已知用户基本信息" in msgs2[0]["content"]
                and "用户长期记忆" in msgs2[0]["content"])

    ok &= check("空 profile 不注入空块",
                "已知用户基本信息" not in
                agent._build_messages(message="hi", profile={})[0]["content"])

    # ---------------------------------------------------------
    # 签名顺序安全
    # ---------------------------------------------------------
    print("\n===== 签名顺序（防位置参数错位）=====")
    import inspect
    p1 = list(inspect.signature(Agent.chat).parameters)
    p2 = list(inspect.signature(Agent.stream).parameters)
    ok &= check("Agent.chat: profile 紧随 memories",
                p1[:6] == ["self", "message", "history", "summary", "memories", "profile"], str(p1[:6]))
    ok &= check("Agent.stream: profile 紧随 memories",
                p2[:6] == ["self", "message", "history", "summary", "memories", "profile"], str(p2[:6]))

    print("\n" + ("全部通过 ✅" if ok else "存在失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
