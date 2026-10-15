"""current_time 工具：返回指定时区的当前日期时间。

用于「现在几点」「东京现在什么时间」这类问题；也方便和多步工具链配合
（例如先 current_time 拿到东京时间，再 calculator 做时区换算）。

默认时区 Asia/Shanghai（产品面向中文用户）。接受任意 IANA 时区名，
如 Asia/Tokyo、America/New_York、UTC、Europe/London。
"""

from datetime import datetime

try:
    from zoneinfo import ZoneInfo

    _ZONEINFO_OK = True
except Exception:  # pragma: no cover - 极低概率环境
    ZoneInfo = None
    _ZONEINFO_OK = False

DEFAULT_TZ = "Asia/Shanghai"

_WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def current_time(timezone: str = DEFAULT_TZ) -> str:
    """
    返回指定时区的当前日期时间。

    Args:
        timezone: IANA 时区名，默认 Asia/Shanghai。

    Returns:
        人类可读的日期/时间/星期字符串；时区无效时返回说明。
    """
    tz = (timezone or DEFAULT_TZ).strip()
    if not _ZONEINFO_OK:
        return "当前环境不支持 zoneinfo，无法计算时区时间。"

    try:
        z = ZoneInfo(tz)
    except Exception as e:
        return f"不支持的时区：{tz}（{e}）"

    now = datetime.now(z)
    return (
        f"时区：{tz}\n"
        f"日期：{now.strftime('%Y-%m-%d')}\n"
        f"时间：{now.strftime('%H:%M:%S')}\n"
        f"星期：{_WEEKDAYS_CN[now.weekday()]}"
    )


# ---------------------------------------------------------------------------
# 工具注册元数据（registry 自动发现，见 app/tool/spec.py）
# ---------------------------------------------------------------------------

TOOL_SPEC = {
    "description": (
        "返回指定时区的当前日期、时间和星期。"
        "用于「现在几点」「东京现在什么时间」等问题。"
        "默认 Asia/Shanghai。"
    ),
    "parameters": {
        "timezone": {
            "type": "string",
            "description": (
                "IANA 时区名，例如 Asia/Shanghai、Asia/Tokyo、"
                "America/New_York、UTC、Europe/London"
            ),
        },
    },
    "required": ["timezone"],
}

TOOL_CONFIG = {
    # 本地计算，毫秒级；失败几乎只会是时区名非法 -> 不重试
    "timeout": 5,
    "retries": 0,
    "quota_per_minute": 120,
}
