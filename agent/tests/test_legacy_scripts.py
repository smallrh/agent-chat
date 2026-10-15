"""把根目录的一次性验证脚本收编进 pytest，**不改动脚本本身**。

现有脚本（`_test_*.py`）都是「脚本式」：末尾有 `if __name__ == "__main__": sys.exit(main())`。
直接把它们搬进 pytest 需要逐个改造成 test 函数，成本高还容易改坏。

这里用「按文件名加载模块 → 调它的 main() → 断言返回 0」的方式，
一行脚本都不用改就能进 CI。等哪天重写某个脚本了，把它从下面的列表里删掉即可。

注意：下面只收录**离线**脚本（用 Fake 依赖，不连真实 PG/Redis/Qdrant）。
如果某个脚本需要真实基础设施，不要加进来。
"""

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

LEGACY_SCRIPTS = [
    "_test_tool_layer.py",          # 工具层插件化（84 项断言）
    "_test_react_loop.py",          # ReAct 多步工具循环
    "_test_tasks.py",               # 后台任务 lifecycle
    "_test_converge.py",            # 记忆 slot 收敛
    "_test_cross_session_memory.py",  # 跨会话记忆 + profile
]


def _load(script_name: str):
    path = ROOT / script_name
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"legacy_{path.stem}", path
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # 让脚本能 import app.*（项目根在 sys.path 上）
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script_name", LEGACY_SCRIPTS)
def test_legacy_script(script_name: str):
    module = _load(script_name)
    if module is None:
        pytest.skip(f"脚本不存在：{script_name}")

    main = getattr(module, "main", None)
    if main is None:
        pytest.skip(f"脚本没有 main()：{script_name}")

    exit_code = main()
    assert exit_code == 0, f"{script_name} 返回非零退出码：{exit_code}"
