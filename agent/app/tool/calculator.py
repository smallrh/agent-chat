"""calculator 工具：安全地计算数学表达式。

安全策略：用 ast 解析表达式，只允许
  - 数字常量
  - 二元运算 + - * / // % **
  - 一元正负号
  - 白名单函数 abs / round / min / max / sqrt / pow / log / sin / cos / tan / ceil / floor
禁止属性访问、下标、import、任意函数调用等，避免代码执行风险。

返回字符串（工具结果统一为字符串，便于直接塞回 LLM）。
"""

import ast
import math
import operator
from typing import Any

_ALLOWED_BINOPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_ALLOWED_UNARY = (ast.UAdd, ast.USub)

_ALLOWED_FUNCS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "pow": pow,
    "log": math.log,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "ceil": math.ceil,
    "floor": math.floor,
}


def _eval_node(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant):
        # 保留 int / float 类型：round(3.14, 2) 的第二个参数必须是 int
        if isinstance(node.value, bool):
            raise ValueError("不支持布尔常量")
        if isinstance(node.value, int):
            return int(node.value)
        if isinstance(node.value, float):
            return float(node.value)
        raise ValueError("只允许数字常量")

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARY):
        val = _eval_node(node.operand)
        return val if isinstance(node.op, ast.UAdd) else -val

    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return _ALLOWED_BINOPS[type(node.op)](left, right)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("不支持的函数调用形式")
        name = node.func.id
        if name not in _ALLOWED_FUNCS:
            raise ValueError(f"不支持的函数：{name}")
        args = [_eval_node(a) for a in node.args]
        return _ALLOWED_FUNCS[name](*args)

    raise ValueError("表达式包含不支持的语法")


# Python 3.11+ 对 int → str 有位数上限（sys.get_int_max_str_digits，默认 4300）。
# 22**5000 有 6743 位，直接 f"{value}" 会抛 ValueError。
# 这里用 bit_length 估算位数（不真正转字符串，避免触发该限制），超限则降级提示。
_MAX_INT_BITS = 13_000          # ≈ 3913 位十进制，留一点余量


def _int_digits(n: int) -> int:
    """估算正整数 n 的十进制位数（不通过 str(n)，避免 int_max_str_digits 限制）。"""
    if n <= 0:
        return 1
    # log10(2) ≈ 0.30102999566
    return int(n.bit_length() * 0.30102999566) + 1


def calculator(expression: str) -> str:
    """
    计算一个数学表达式。

    Args:
        expression: 例如 "2 + 3 * 4"、"sqrt(16)"、"pow(2, 10)"、"round(3.14159, 2)"

    Returns:
        计算结果（字符串）。失败时返回人类可读的错误说明。
    """
    try:
        expr = str(expression).strip()
        if not expr:
            return "表达式为空。"
        tree = ast.parse(expr, mode="eval")
        value = _eval_node(tree.body)
        # 整数结果不显示 .0
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        # 大整数：直接 f-string 会抛 ValueError，先按位数降级
        if isinstance(value, int) and value.bit_length() > _MAX_INT_BITS:
            return (
                f"结果是一个约 {_int_digits(value)} 位的整数，超出可展示范围。"
                f"如需近似值，请说明需要的有效位数。"
            )
        return f"{value}"
    except Exception as e:
        return f"计算失败：{e}"


# ---------------------------------------------------------------------------
# 工具注册元数据（registry 自动发现，见 app/tool/spec.py）
# ---------------------------------------------------------------------------

TOOL_SPEC = {
    "description": (
        "计算数学表达式。支持 + - * / // % ** 以及"
        " abs/round/min/max/sqrt/pow/log/sin/cos/tan/ceil/floor。"
        "用于需要精确数值计算的场景，例如「2 的 10 次方是多少」"
        "「3.14159 保留两位」「sqrt(144)」。"
    ),
    "parameters": {
        "expression": {
            "type": "string",
            "description": "数学表达式，例如 \"2 + 3 * 4\"、\"sqrt(16)\"、\"pow(2, 10)\"",
        },
    },
    "required": ["expression"],
}

TOOL_CONFIG = {
    # 纯函数：失败一定是表达式不合法，重试没有意义 -> 关掉重试快速失败
    "timeout": 5,
    "retries": 0,
    "quota_per_minute": 120,
}
