"""工具元信息（spec）与运行配置（config）。

## 约定

在 `app/tool/` 下新建一个 Python 文件即可注册工具，registry 会自动发现。
模块里可以提供以下模块级变量（全部可选，只有"实现函数"是必需的）：

| 变量 | 作用 | 缺省行为 |
|---|---|---|
| `TOOL_NAME` | 工具名（LLM 看到的 function name） | 用模块名；再退化用函数名 |
| `TOOL_SPEC` | OpenAI function-calling 定义片段 | 见下 |
| `TOOL_CONFIG` | 运行配置（超时/重试/熔断/配额） | `ToolConfig()` 默认值 |
| `TOOL_IMPL` | 显式指定实现对象 | 依次试 `TOOL_NAME` → 同名函数 → 唯一可调用对象 |

`TOOL_SPEC` 支持两种写法（推荐第 1 种，简洁）：

```python
TOOL_SPEC = {
    "description": "查询指定城市的天气。",
    "parameters": {
        "city": {"type": "string", "description": "城市名称"},
        "time_range": {"type": "string", "enum": ["current", "today", "week"]},
    },
    "required": ["city"],           # 可选，缺省时全部参数均为必填
}
```

也可以直接给完整的 OpenAI 结构（`{"name": ..., "description": ..., "parameters": {...}}`），
registry 会原样采用，方便从别处迁移既有定义。

**不被当作工具的情况**：没有上述任何模块级变量，或同名对象不可调用。
`context.py` / `registry.py` / `spec.py` / `guard.py` 这些 infra 模块因此天然被跳过。

**失败隔离**：单个模块的 import 异常不影响其它工具，registry 会记录 ERROR 日志后跳过。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 运行配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolConfig:
    """一个工具的运行约束。

    数值全部以「保守」为默认取向：宁可早失败返回可读文案，也不要拖垮一次对话。
    """

    # 是否启用。False 时既不注册给 LLM，也不允许调用。
    enabled: bool = True

    # 单次调用超时（秒）。超过即放弃等待并返回错误文案。
    # 注意：同步工具跑在工作线程里，Python 无法强杀线程 —— 超时只表示"调用方不等了"，
    # 线程里的代码仍会继续跑到结束。因此**工具自身必须内置自己的超时/资源上限**
    # （例如 code_runner 的 subprocess timeout），这一层只是外部兜底。
    timeout: float = 20.0

    # 失败重试次数（不含首次调用）。0 = 不重试。
    retries: int = 1
    # 指数退避基数与封顶（秒）。
    retry_backoff: float = 0.5
    retry_max_backoff: float = 2.0

    # 熔断：连续失败多少次后开闸，开闸后 circuit_reset 秒内直接拒绝（不再打下游）。
    circuit_threshold: int = 5
    circuit_reset: float = 60.0

    # 配额：单个 user_id 每分钟最多调几次。0 = 不限。
    quota_per_minute: int = 60


# ---------------------------------------------------------------------------
# 工具元信息
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolMeta:
    """工具的全部静态信息：名字、实现、给 LLM 的定义、运行配置。"""

    name: str
    impl: Callable[..., Any]
    definition: dict[str, Any]          # 完整 OpenAI function 结构
    config: ToolConfig = field(default_factory=ToolConfig)
    module: str = ""

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.impl)


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------


def parse_config(raw: Any) -> ToolConfig:
    """把模块里的 TOOL_CONFIG（dict 或 ToolConfig）解析成 ToolConfig。

    未知字段直接忽略，避免写错一个键就整个注册失败。
    """
    if raw is None:
        return ToolConfig()
    if isinstance(raw, ToolConfig):
        return raw
    if not isinstance(raw, dict):
        return ToolConfig()

    known = {
        "enabled": bool,
        "timeout": float,
        "retries": int,
        "retry_backoff": float,
        "retry_max_backoff": float,
        "circuit_threshold": int,
        "circuit_reset": float,
        "quota_per_minute": int,
    }
    kwargs: dict[str, Any] = {}
    for key, caster in known.items():
        if key in raw:
            try:
                kwargs[key] = caster(raw[key])
            except (TypeError, ValueError):
                # 单个字段写坏不该拖垮整个工具，回落默认值
                continue
    return ToolConfig(**kwargs)


# ---------------------------------------------------------------------------
# OpenAI function 结构归一化
# ---------------------------------------------------------------------------

_JSON_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def build_definition(
    name: str,
    spec: dict[str, Any],
    impl: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """把各种写法的 TOOL_SPEC 归一化成 OpenAI function 结构。

    支持三种输入：
      1. `{"description": ..., "parameters": {字段名: 字段定义}, "required": [...]}`
         —— 简写版，最常用
      2. `{"name": ..., "description": ..., "parameters": {"type": "object",
           "properties": {...}, "required": [...]}}` —— OpenAI 原生版
      3. 只给 `"description"`，没给 `"parameters"` —— 由 impl 的函数签名推断

    无论哪种，输出都保证是：
        {"type": "function", "function": {"name", "description", "parameters"}}
    """
    spec = dict(spec or {})
    description = str(spec.get("description") or "").strip()
    parameters = spec.get("parameters")

    # 形态 1：parameters 是一个「字段名 → schema」的扁平 dict（没有 "type"/"properties" 键）
    is_short_form = (
        isinstance(parameters, dict)
        and "type" not in parameters
        and "properties" not in parameters
    )
    if is_short_form:
        properties = dict(parameters)
        required = list(spec.get("required") or [])
        parameters = {
            "type": "object",
            "properties": properties,
            "required": required or _infer_required(impl, properties),
        }
    elif isinstance(parameters, dict):
        parameters = {
            "type": "object",
            "properties": dict(parameters.get("properties") or {}),
            "required": list(parameters.get("required") or []),
        }
    else:
        # 形态 3：从函数签名兜底
        parameters = infer_parameters(impl)

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def _infer_required(impl: Callable[..., Any] | None, properties: dict) -> list[str]:
    """简写形态下没写 required 时：凡是没有默认值的参数都算必填。"""
    if impl is None:
        return []
    try:
        sig = inspect.signature(impl)
    except (TypeError, ValueError):
        return []
    return [
        name
        for name, param in sig.parameters.items()
        if name in properties
        and param.default is inspect.Parameter.empty
        and name not in ("user_id", "conversation_id")   # 这两个由 ToolContext 注入
    ]


def infer_parameters(impl: Callable[..., Any] | None) -> dict[str, Any]:
    """从函数签名推断 JSON Schema（兜底用，手写 TOOL_SPEC 时不会走到这里）。

    只能给出类型，description 拿不到 —— 所以想让 LLM 调得准，还是手写 TOOL_SPEC。
    """
    if impl is None:
        return {"type": "object", "properties": {}, "required": []}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in inspect.signature(impl).parameters.items():
        # 这两个由 ToolContext 注入，不暴露给 LLM
        if name in ("user_id", "conversation_id"):
            continue
        annotation = param.annotation
        properties[name] = {"type": _JSON_TYPES.get(annotation, "string")}
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {"type": "object", "properties": properties, "required": required}
