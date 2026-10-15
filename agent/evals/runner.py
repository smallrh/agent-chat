"""评测集总入口（P3）。

为什么要有它：
    P0 做的「搜索强制引用 + 禁止编造」、P1 做的「相似 query 不二次搜索」、
    P2 做的「真任务 worker / 附件注入」，全是靠人工跑一遍确认的。
    没有评测集 = 下次改提示词或换模型时**没有任何东西会告诉你回归了**。
    《现状和不足.md》把「评测集：酒店/天气/计算黄金问答，防回归幻觉」
    放在 P3，这里按「离线快评 + 在线全评」两层实现。

两层设计
--------
offline（默认，秒级，CI 可跑）
    不调用模型，只断言**确定性组件**：grounding 的引用校验与降级、
    搜索去重的命中规则、搜索预算、任务 worker 的脏输入容错、
    附件的路径穿越防护与多模态内容构造。
    这些是「只要代码改坏了就一定红」的硬断言。

online（--layer online，需 LM Studio 在线）
    真实跑完整 Agent（多步工具循环），对黄金问答做断言：
    工具是否命中、数值是否正确、**检索无果时是否老实说不知道**。
    这层反映真实效果，但依赖模型，是「周更/发版前手动跑」的。

用法
----
    .venv/Scripts/python.exe -m evals.runner                # 只跑离线
    .venv/Scripts/python.exe -m evals.runner --layer all    # 两层都跑
    .venv/Scripts/python.exe -m evals.runner --layer online --case calc-1

退出码：全部通过 0；有失败 1。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import sys
import time
import traceback
from dataclasses import dataclass, field


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

# Windows 控制台默认不认 ANSI；能开就开，开不了也不影响结果
if sys.platform == "win32":  # pragma: no cover
    try:  # noqa: SIM105
        import ctypes

        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    except Exception:
        pass


@dataclass
class CaseResult:
    case_id: str
    layer: str
    passed: bool
    duration_ms: float
    message: str = ""
    detail: str = ""

    @property
    def status(self) -> str:
        if self.passed:
            return f"{GREEN}PASS{RESET}"
        return f"{RED}FAIL{RESET}"


@dataclass
class LayerReport:
    layer: str
    results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    def print(self) -> None:
        print(f"\n{'=' * 62}")
        print(f"  层：{self.layer}   通过 {self.passed} / {len(self.results)}")
        print("=" * 62)
        for r in self.results:
            ms = f"{r.duration_ms:.0f}ms"
            print(f"  [{r.status}] {r.case_id} {DIM}({ms}){RESET}")
            if r.message:
                print(f"         {r.message}")
            if not r.passed and r.detail:
                for line in r.detail.splitlines()[-6:]:
                    print(f"         {DIM}{line}{RESET}")


# ---------------------------------------------------------------------------
# 离线层：收集 cases_offline.py 里的 test_* 函数
# ---------------------------------------------------------------------------


def run_offlayer(case_filter: str | None = None) -> LayerReport:
    from evals import cases_offline

    report = LayerReport("offline")
    for name in dir(cases_offline):
        if not name.startswith("test_"):
            continue
        if case_filter and case_filter not in name:
            continue
        fn = getattr(cases_offline, name)
        if not callable(fn):
            continue

        case_id = name[len("test_") :]
        started = time.perf_counter()
        try:
            if inspect.iscoroutinefunction(fn):
                msg = asyncio.run(fn())
            else:
                msg = fn()
            report.results.append(
                CaseResult(
                    case_id=case_id,
                    layer="offline",
                    passed=True,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    message=msg or "",
                )
            )
        except AssertionError as e:
            report.results.append(
                CaseResult(
                    case_id=case_id,
                    layer="offline",
                    passed=False,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    message=str(e) or "断言失败",
                    detail=traceback.format_exc(),
                )
            )
        except Exception as e:  # noqa: BLE001
            report.results.append(
                CaseResult(
                    case_id=case_id,
                    layer="offline",
                    passed=False,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    message=f"{type(e).__name__}: {e}",
                    detail=traceback.format_exc(),
                )
            )
    return report


# ---------------------------------------------------------------------------
# 在线层：跑黄金问答
# ---------------------------------------------------------------------------


def run_onlayer(case_filter: str | None = None) -> LayerReport | None:
    """跑黄金问答。LLM 不可达时返回 None（整层跳过，不计入失败）。"""

    from evals import cases_online

    if not cases_online.llm_available():
        print(
            f"\n{YELLOW}⚠ 在线层已跳过：LLM 不可达（LM Studio 没开或地址不通）。"
            f"这不是失败 —— 离线层照常跑完。{RESET}"
        )
        return None

    report = LayerReport("online")
    for case in cases_online.GOLDEN_CASES:
        if case_filter and case_filter not in case["id"]:
            continue
        started = time.perf_counter()
        try:
            res = cases_online.run_case(case)
            # run_case 是 async：需要在事件循环里把同步 LLM 调用丢到线程
            if inspect.isawaitable(res):
                res = asyncio.run(res)
            passed, msg, detail = res
        except Exception as e:  # noqa: BLE001
            passed, msg, detail = False, f"{type(e).__name__}: {e}", traceback.format_exc()
        report.results.append(
            CaseResult(
                case_id=case["id"],
                layer="online",
                passed=passed,
                duration_ms=(time.perf_counter() - started) * 1000,
                message=msg,
                detail=detail,
            )
        )
    return report


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 评测集（P3）")
    parser.add_argument(
        "--layer",
        choices=["offline", "online", "all"],
        default="offline",
        help="offline=确定性组件（默认，不需要模型）；online=黄金问答（需要模型）",
    )
    parser.add_argument("--case", default=None, help="只跑 id 包含该子串的用例")
    args = parser.parse_args()

    print(f"{DIM}Agent 评测集 · layer={args.layer}{RESET}")

    reports: list[LayerReport] = []
    if args.layer in ("offline", "all"):
        reports.append(run_offlayer(args.case))
    if args.layer in ("online", "all"):
        # 模型不在线时 run_onlayer 返回 None（跳过，不算失败）
        online = run_onlayer(args.case)
        if online is not None:
            reports.append(online)

    for r in reports:
        r.print()

    total = sum(len(r.results) for r in reports)
    failed = sum(r.failed for r in reports)
    print(f"\n{'=' * 62}")
    if failed == 0:
        print(f"  {GREEN}全部通过 ✅{RESET}  {total} 条用例")
    else:
        print(f"  {RED}{failed} 条失败 ❌{RESET} / 共 {total} 条")
    print("=" * 62)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
