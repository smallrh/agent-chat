
---

## 8. 问题与修复记录

> 记录真实踩到的坑与处置，避免重复排查。

### 2026-10-09：三连问「天气 + 22^5000 + 琶洲岛」明显变慢

**现象**：一次提问含三个子任务，端到端耗时远超预期。

**排查**：写 `_diag_parallel.py` 分别测「并发 vs 串行」与单工具耗时。

**结论**：并行代码没问题 —— 3 个 200ms 工具，串行 605ms、并发 201ms，加速比 **3.00x**。
（注意：毫秒级工具测不出并发收益，线程调度开销会盖过，必须用 200ms+ 的慢工具测。）

**三个真因**：

1. **calculator 大整数崩溃 → 模型重试风暴**（主因）
   `22**5000` 有 6713 位；CPython 3.11+ 限制 `int→str` ≤ 4300 位，
   `return f"{value}"` 直接抛 `ValueError`，被 `except Exception` 吞成"计算失败"。
   模型看不到根因 → 反复换工具（calculator → code_runner → calculator…），
   每轮一次完整 LLM 往返，最多吃满 `MAX_TOOL_ROUNDS=10`。

   处置：`calculator` 转字符串前用 `bit_length` 估算位数（不真正转字符串，规避限制），
   超限返回「结果是一个约 N 位的整数，超出可展示范围」。

2. **Exa 联网搜索单次 33.21 秒**（外部 API 固有延迟）
   并行省的是"叠加"，省不掉"最大值"——搜索天然最慢，它就是那轮的下限。

   处置：`TOOL_CONFIG.timeout` 30 → 60（原超时必然触发），
   `retries` 1 → 0（超时后重试只会再吃 60s 且仍失败）。

3. **若模型一轮只返回 1 个 tool_call，并行分支不触发** → 退化串行多轮
   待验证：看日志里 3 个 `tool.start` 是否同时出现。

### 2026-10-09：Exa 官方流式写法在本环境崩溃

**现象**：按官方示例 `client.responses.create(..., stream=True)` 调用，HTTP 200 但抛

    JSONDecodeError: Expecting value: line 1 column 1 (char 0)

**根因**：Exa 的 SSE 流里夹了 `: keep-alive` 注释行。openai SDK 的
`openai/_streaming.py` 对该行拿到的 data 缓冲区为空 → `json.loads("")` 崩。
且异常发生在 async generator 内部，**外层 try/except 接不到**。

（用裸 httpx 抓原始流确认：HTTP 200，事件完整，仅多了 `: keep-alive` 注释行。）

**处置**：保持非流式，改为 httpx 直连 Exa Responses API + 手写宽容 SSE 解析
（跳过注释行、空 data、不可解析事件）。原因已写进 `app/tool/web_search.py` 顶部文档。

### 2026-10-09：连打多个补丁脚本把 guard.py 写坏

**现象**：改 `guard.py` 时出现三次连续错误——
① 同步 `run()` 被改成 `await`（SyntaxError）；② 漏 `_describe`；③ 漏 `_blocked`。

**根因**：无 `Edit` 工具，改用「补丁脚本 + python 执行」；
对一个文件做**多处结构性改动**时连打几个补丁，补丁之间互相干扰且难回滚。

**教训**：单文件多处结构性改动 → 宁可先读完整文件再 Write 整体重写，
并且重写前**逐项核对原文件的内容清单**（函数、常量、模块级变量），
不要凭记忆重写。本次就是重写时漏掉了 `TOOL_SPEC`/`TOOL_CONFIG`，
导致 `web_search` 被 registry 跳过。
