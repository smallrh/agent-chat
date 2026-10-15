import io

p = "app/tool/guard.py"
s = io.open(p, encoding="utf-8").read()

if "    def _blocked(" in s:
    print("[skip] _blocked already defined")
    raise SystemExit(0)

block = '''    @staticmethod
    def _blocked(reason: str, meta: ToolMeta, started: float) -> GuardResult:
        """构造一个「被拦截」的结果（不执行工具，直接给模型可读文案）。"""
        text = {
            "disabled": f"工具已停用：{meta.name}",
            "quota": f"工具调用过于频繁，请稍后再试：{meta.name}",
            "circuit": f"工具暂时不可用（连续失败过多，已自动熔断）：{meta.name}",
        }[reason]
        result = GuardResult(ok=False, text=text, attempts=0, blocked=reason)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "tool blocked: name=%s reason=%s elapsed_ms=%d",
            meta.name, reason, result.elapsed_ms,
        )
        return result

'''

marker = "    # ------------------------------------------------------------------\n    # 后置：状态更新 + 审计"
assert marker in s, "record marker missing"
s = s.replace(marker, block + marker, 1)

io.open(p, "w", encoding="utf-8").write(s)
print("[ok] guard.py + _blocked")
