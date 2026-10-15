import asyncio

from app.service.task_service import TaskLimitReached, task_service


async def create_task(
    title: str,
    goal: str = "",
    kind: str = "research",
    user_id: int | None = None,
    conversation_id: str | None = None,
) -> str:
    """
    创建一个「后台任务」并立即异步运行，不阻塞当前对话。

    适用场景：需要多步、耗时、可并行的任务（例如资料搜集整理、长文撰写、
    批量处理）。任务会在后台独立运行，进度通过任务流（/tasks/stream）实时
    推送；完成后结果会自动写回当前会话（作为一条 assistant 消息）。
    本工具立刻返回 task_id，你可以继续和用户对话，无需等待任务完成。

    参数：
      - title: 任务标题（一句话说明要做什么）
      - goal:  任务目标/要求（可选，越具体越好）
      - kind:  任务类型：
                - "research"（默认）：真实调研 worker，会拆解子问题、
                  多次联网检索、再汇总成带参考来源的中文报告。
                - "demo"：内置演示 worker（纯模拟步骤，不发网络请求），
                  仅在只想验证任务链路是否能跑通时使用。
      - user_id / conversation_id: 由系统自动注入，无需填写

    并发限制：同一用户同时最多 3 个任务在跑，超限会返回失败提示，
    此时应告诉用户「已有多个任务在进行，请稍后再试」，不要反复重试创建。
    """
    if not conversation_id:
        return "无法创建任务：缺少 conversation_id（请确保在对话上下文中调用）。"
    if not title:
        return "无法创建任务：title 不能为空。"

    # 这个协程运行在主事件循环上，asyncio.create_task 由 TaskService 调度，
    # 不会阻塞本次工具返回。
    try:
        task_id = await task_service.spawn_task(
            conversation_id=conversation_id,
            user_id=user_id,
            title=title,
            goal=goal,
            kind=kind,
        )
    except ValueError as e:
        return f"创建任务失败：{e}"
    except TaskLimitReached:
        return (
            "创建任务失败：当前已有太多任务在运行（单用户并发上限）。"
            "请告诉用户稍等当前任务完成后再试，不要立即重试。"
        )
    except Exception as e:  # noqa: BLE001
        return f"创建任务失败：{e}"

    return (
        f"已创建后台任务（task_id={task_id}，类型 {kind}）。该任务正在后台运行，"
        f"进度会通过任务流实时推送，完成后结果自动写回本会话。"
        f"你现在可以继续和用户对话，不要空等结果。"
    )


# ---------------------------------------------------------------------------
# 工具注册元数据（registry 自动发现，见 app/tool/spec.py）
# ---------------------------------------------------------------------------

TOOL_SPEC = {
    "description": (
        "创建一个后台任务并立即异步运行，不阻塞当前对话。"
        "适用于需要多步、耗时、可并行的工作（如资料搜集整理、长文撰写、"
        "批量处理）。任务在后台独立运行，进度通过任务流实时推送，"
        "完成后结果自动写回当前会话（作为一条 assistant 消息）。"
        "调用后立即返回 task_id，可继续与用户对话。"
        "参数 title 必填；goal 越具体越好；"
        "kind 默认 research（真实调研 worker：拆解子问题→联网检索→"
        "汇总成带参考来源的报告），只有想验证任务链路本身时才用 demo。"
    ),
    "parameters": {
        "title": {
            "type": "string",
            "description": "任务标题，一句话说明要做什么",
        },
        "goal": {
            "type": "string",
            "description": "任务目标 / 要求（可选，越具体越好）",
        },
        "kind": {
            "type": "string",
            "description": (
                "任务类型：research（默认，真实联网调研）或 "
                "demo（纯模拟步骤，不发网络请求）"
            ),
            "enum": ["research", "demo"],
        },
    },
    "required": ["title"],
}

TOOL_CONFIG = {
    # 写操作：重试会导致重复建任务 -> 必须关掉重试
    "timeout": 20,
    "retries": 0,
    "quota_per_minute": 20,
}
