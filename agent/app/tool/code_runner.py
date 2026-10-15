"""code_runner 工具：在隔离环境中执行 Python 代码片段。

用于数据处理、数值计算、简单算法验证等场景，是「工具链式」演示的利器
（例如 weather 拿到温度 → code_runner 做时区/单位换算）。

安全说明（仅限开发调试用途）：
- 使用 `python -I`（隔离模式：不加载 site-packages、不读用户 site），
  尽量缩小可用面。
- 通过子进程 + 超时执行，避免死循环卡死主进程。
- 仍有执行任意代码的能力，生产环境请勿对不可信输入开放。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile

from app.core.config import settings


logger = logging.getLogger(__name__)


# 超时硬边界：timeout 由 LLM 传入，schema 里没有上界，不对它做钳制的话
# 模型完全可以传个 999999 长期占住 worker 线程。
MAX_TIMEOUT_SECONDS = int(
    getattr(settings, "CODE_RUNNER_MAX_TIMEOUT", 10) or 10,
)
MIN_TIMEOUT_SECONDS = 1


def code_runner(code: str, timeout: int = 10) -> str:
    """
    执行一段 Python 代码并返回其标准输出 / 标准错误。

    Args:
        code:    要执行的 Python 代码。
        timeout: 超时秒数，默认 10；会被钳到 [1, CODE_RUNNER_MAX_TIMEOUT]。

    Returns:
        裁剪后的输出；空输出 / 超时报错时返回可读说明。
    """
    # 总开关：默认关闭。真实生产力用途请在 .env 显式开启，
    # 并放进禁用网络/只读的独立容器里跑。
    if not getattr(settings, "ENABLE_CODE_RUNNER", False):
        return (
            "code_runner 当前已关闭（ENABLE_CODE_RUNNER=false）。"
            "如需使用请在 .env 开启，并确认运行在隔离环境中。"
        )

    if not code or not code.strip():
        return "代码为空。"

    if len(code) > 5000:
        return "代码过长（上限 5000 字符）。"

    # 超时钳制：非法入参一律回落到默认上限内的安全值
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = MAX_TIMEOUT_SECONDS
    timeout = max(MIN_TIMEOUT_SECONDS, min(timeout, MAX_TIMEOUT_SECONDS))

    path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            path = f.name

        # sys.executable：用当前解释器，而不是 PATH 上的某个 python。
        # 后者在热重载/多环境里可能不是本项目 venv，造成依赖缺失或行为漂移
        # （与之前的「双实例 + 解释器不一致」属同一类坑）。
        proc = subprocess.run(
            [sys.executable, "-I", path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            out = f"（退出码 {proc.returncode}）\n" + out

    except subprocess.TimeoutExpired:
        return f"代码执行超时（>{timeout}s），已终止。"
    except Exception as e:
        return f"代码执行失败：{e}"
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    if not out.strip():
        return "（无输出）"
    return out[:2000]


# ---------------------------------------------------------------------------
# 工具注册元数据（registry 自动发现，见 app/tool/spec.py）
# ---------------------------------------------------------------------------

TOOL_SPEC = {
    "description": (
        "在隔离环境中执行 Python 代码片段并返回标准输出/错误，"
        "用于数据处理、计算、简单算法验证等。仅限开发调试用途。"
    ),
    "parameters": {
        "code": {
            "type": "string",
            "description": "要执行的 Python 代码",
        },
        "timeout": {
            "type": "integer",
            "description": "超时秒数，默认10",
        },
    },
    "required": ["code"],
}

TOOL_CONFIG = {
    # 已知待办：LLM 传入的 timeout 参数目前没在函数内做上界钳制，
    # 这里的外层 15s 是兜底（超时后只代表调用方不等了，线程仍在跑）。
    # 修完 code_runner.timeout 的钳制后可放宽 retry 策略。
    "timeout": 15,
    # 执行代码可能有副作用，重试会造成重复执行 -> 关掉重试
    "retries": 0,
    "quota_per_minute": 20,
}
