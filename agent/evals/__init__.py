"""评测集（P3）：离线快评 + 在线全评。

    offline —— 不调模型，断言确定性组件（默认）
    online  —— 真实跑 Agent 的黄金问答（需 LM Studio）

用法：
    .venv/Scripts/python.exe -m evals.runner [--layer offline|online|all]

注意：`__init__.py` 里刻意**不导入 runner** —— 否则 `python -m evals.runner`
会先执行包（导入 runner 进 sys.modules）再执行模块，触发
"found in sys.modules after import of package" 警告并导致行为不可预期。
"""

__all__: list[str] = []
