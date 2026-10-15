"""工具注册表：自动发现 app/tool/ 下的工具模块。

## 为什么改成自动发现

以前加一个工具要改 3 处：这里的 import、`TOOLS`、`TOOL_DEFINITIONS`。
现在只需要**在 app/tool/ 下新建一个文件**，见 app/tool/spec.py 顶部的约定说明。

## 对外接口（保持向后兼容）

    TOOLS             dict[str, Callable]  tool_executor 用它取实现
    TOOL_DEFINITIONS  list[dict]           llm.py 用它给模型下发 function schema
    TOOL_METAS        dict[str, ToolMeta]  含实现 + schema + 运行配置（新增）

另有：`register()` 手动注册、`reload()` 重新扫描、`get_meta()`、`disable()`/`enable()`。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import replace
from typing import Any, Callable

# 包路径：__path__ 只在包的 __init__ 里可见，子模块必须显式取
from app.tool import __path__ as TOOL_PACKAGE_PATH
from app.core.config import settings
from app.tool.spec import (
    ToolConfig,
    ToolMeta,
    build_definition,
    parse_config,
)

logger = logging.getLogger(__name__)


# infra 模块：即使将来误加了标记也不注册为工具
_SKIP_MODULES = {"__init__", "context", "registry", "spec", "guard"}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

TOOL_METAS: dict[str, ToolMeta] = {}


def default_config() -> ToolConfig:
    """模块没写 TOOL_CONFIG 时的全局默认值（从 settings 读，方便统一收紧/放宽）。"""
    return ToolConfig(
        timeout=float(getattr(settings, "TOOL_DEFAULT_TIMEOUT", 20.0)),
        retries=int(getattr(settings, "TOOL_MAX_RETRIES", 1)),
        retry_backoff=float(getattr(settings, "TOOL_RETRY_BACKOFF", 0.5)),
        circuit_threshold=int(getattr(settings, "TOOL_CIRCUIT_THRESHOLD", 5)),
        circuit_reset=float(getattr(settings, "TOOL_CIRCUIT_RESET_SECONDS", 60.0)),
        quota_per_minute=int(getattr(settings, "TOOL_DEFAULT_QUOTA_PER_MINUTE", 60)),
    )


def register(
    name: str,
    impl: Callable[..., Any],
    spec: dict[str, Any] | None = None,
    config: ToolConfig | dict[str, Any] | None = None,
    *,
    module: str = "",
    replace_existing: bool = False,
) -> ToolMeta | None:
    """手动注册一个工具。

    用于：定义在 app/tool 包之外的工具（插件包、运行时动态生成），
    或单测里塞一个假工具进来。

    Returns:
        ToolMeta，或 None（参数不合法）。
    """
    if not name or not callable(impl):
        logger.error("register skipped: invalid tool: name=%r impl=%r", name, impl)
        return None
    if name in TOOL_METAS and not replace_existing:
        logger.warning("register: tool already exists, skipped: %s", name)
        return TOOL_METAS[name]

    meta = ToolMeta(
        name=name,
        impl=impl,
        definition=build_definition(name, spec or {}, impl),
        config=parse_config(config) if config is not None else default_config(),
        module=module,
    )
    TOOL_METAS[name] = meta
    _sync_compat()
    logger.info(
        "tool registered: name=%s module=%s async=%s enabled=%s "
        "timeout=%.1fs retries=%d quota=%d/min",
        name, module or "-", meta.is_async, meta.config.enabled,
        meta.config.timeout, meta.config.retries, meta.config.quota_per_minute,
    )
    return meta


def get_meta(name: str) -> ToolMeta | None:
    return TOOL_METAS.get(name)


def reload() -> dict[str, ToolMeta]:
    """清空并重新扫描（热重载 / 运维触发）。"""
    TOOL_METAS.clear()
    discover()
    _sync_compat()
    return TOOL_METAS


# ---------------------------------------------------------------------------
# 自动发现
# ---------------------------------------------------------------------------


def _pick_impl(mod: Any, name: str) -> Callable[..., Any] | None:
    """从模块里挑出实现对象。

    优先级：显式 TOOL_IMPL → 与工具同名的可调用对象。
    """
    explicit = getattr(mod, "TOOL_IMPL", None)
    if callable(explicit):
        return explicit

    candidate = getattr(mod, name, None)
    if callable(candidate):
        return candidate
    return None


def discover() -> dict[str, ToolMeta]:
    """扫描 app/tool/ 下所有模块，把带 TOOL_SPEC / TOOL_IMPL 标记的注册进来。

    失败隔离：单个模块 import 炸了只记日志跳过，不影响其它工具，
    也不会让应用起不来。
    """
    if not getattr(settings, "TOOL_DISCOVERY_ENABLED", True):
        logger.warning("tool discovery disabled by config, registry stays empty")
        return TOOL_METAS

    for info in pkgutil.iter_modules(TOOL_PACKAGE_PATH):
        mod_name = info.name
        if mod_name in _SKIP_MODULES or mod_name.startswith("_"):
            continue

        try:
            # 注意：__name__ 是 app.tool.registry（模块自身），包名要用 __package__
            mod = importlib.import_module(f"{__package__}.{mod_name}")
        except Exception:                                  # noqa: BLE001
            logger.exception("tool discovery: import failed, skipped: %s", mod_name)
            continue

        spec = getattr(mod, "TOOL_SPEC", None)
        explicit_impl = getattr(mod, "TOOL_IMPL", None)
        # 必须至少有一个显式标记，避免把包内的普通函数误注册成工具
        if spec is None and explicit_impl is None:
            continue

        name = str(
            getattr(mod, "TOOL_NAME", None)
            or (spec or {}).get("name")
            or mod_name
        )
        impl = _pick_impl(mod, name)
        if impl is None:
            logger.warning(
                "tool discovery: no callable impl found, skipped: module=%s name=%s "
                "(把函数命名成工具名，或显式指定 TOOL_IMPL)",
                mod_name, name,
            )
            continue

        if isinstance(spec, dict) and "name" in spec:
            # name 已单独处理，从 spec 片段里摘掉避免重复
            spec = {k: v for k, v in spec.items() if k != "name"}

        register(
            name=name,
            impl=impl,
            spec=spec,
            config=getattr(mod, "TOOL_CONFIG", None),
            module=mod_name,
            replace_existing=True,
        )

    return TOOL_METAS


# ---------------------------------------------------------------------------
# 兼容旧接口（llm.py / tool_executor.py 依赖这两个名字，勿删）
# ---------------------------------------------------------------------------

TOOLS: dict[str, Callable[..., Any]] = {}
TOOL_DEFINITIONS: list[dict[str, Any]] = []


def _sync_compat() -> None:
    """把 TOOL_METAS 投影成老的 TOOLS / TOOL_DEFINITIONS。

    TOOL_DEFINITIONS 只暴露 enabled 的工具 —— 停用的工具不该出现在发给
    模型的 function schema 里（否则模型会调一个必然失败的工具）。
    """
    TOOLS.clear()
    TOOL_DEFINITIONS.clear()
    for name, meta in TOOL_METAS.items():
        TOOLS[name] = meta.impl
        if meta.config.enabled:
            TOOL_DEFINITIONS.append(meta.definition)


# ---------------------------------------------------------------------------
# 运行时开关（改内存态，重启后回到 TOOL_CONFIG / settings 的声明值）
# ---------------------------------------------------------------------------


def enable(name: str) -> bool:
    meta = TOOL_METAS.get(name)
    if meta is None:
        return False
    object.__setattr__(meta, "config", replace(meta.config, enabled=True))
    _sync_compat()
    return True


def disable(name: str) -> bool:
    meta = TOOL_METAS.get(name)
    if meta is None:
        return False
    object.__setattr__(meta, "config", replace(meta.config, enabled=False))
    _sync_compat()
    logger.warning("tool disabled at runtime: %s", name)
    return True


# ---------------------------------------------------------------------------
# 模块加载即完成发现
# ---------------------------------------------------------------------------

discover()
_sync_compat()

logger.info(
    "tool registry ready: total=%d enabled=%d names=%s",
    len(TOOL_METAS), len(TOOL_DEFINITIONS), sorted(TOOL_METAS),
)

__all__ = [
    "TOOLS",
    "TOOL_DEFINITIONS",
    "TOOL_METAS",
    "register",
    "get_meta",
    "discover",
    "reload",
    "enable",
    "disable",
    "default_config",
    "ToolMeta",
    "ToolConfig",
]
