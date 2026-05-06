# Dataflow Phase 5b — Bridges Cleanup + chat_node split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 Phase 1 引入的 `app/bridges/` Bridge 包；让 `app/life/proactive.py` 直接 emit `Message`；把 `chat_node.py` 的 pre-safety helper 拆出独立模块；清理 4 处 "Task 7-11" docstring + 2 处遗留 `emit_legacy_message` 注释。无新功能，纯结构清理。

**Architecture:**
- Bridge 是 Phase 1-5a 的临时过渡层（spec line 79、line 414-423 已写明 5b 删除）。Phase 5a 完成后 chat 主路径直接 emit `ChatResponseSegment`，proactive 是唯一仍用 Bridge 的写入方，迁完即可清掉 `app/bridges/` 整个包。
- `chat_node.py` 当前 317 行（超 CLAUDE.md 300 行 guideline 17 行）。把 `_PreSafetyResult` + `_resolve_pre_safety_for_part`（共 34 行 helper）拆到 `app/nodes/_chat_pre_safety.py`，主文件落到 ~283 行。

**Tech Stack:** Python 3.11 + pytest + `app.runtime.emit` runtime + 现有 `tests/conftest.py:capture_emit` fixture

**Spec reference:** `docs/superpowers/specs/2026-05-06-dataflow-phase-5-chat-pipeline-design.md` line 414-423（5b 改 proactive + 删 bridges）、line 516（test_proactive 用 capture_emit）、line 590（grep 验收）。

**5a 后续 followup（来自 `project_dataflow_phase5a_shipped.md`）：**
- Phase 5b: 删 `app/bridges/` + proactive.py 改 `emit(Message.from_cm(cm))`
- chat_node.py 当前 317 行；拆 `_resolve_pre_safety_for_part` + `_PreSafetyResult` 到 `app/nodes/_chat_pre_safety.py`
- 4 处 docstring 仍引用 plan 的 "Task 7-11" 用语，5b 时清成语义化阶段名

---

## File Structure

**Create:**
- `apps/agent-service/app/nodes/_chat_pre_safety.py` — pre-safety segment helper（`_PreSafetyResult` + `_resolve_pre_safety_for_part`），从 chat_node.py L40-73 抽出。下划线前缀表示 chat_node 的内部实现细节。
- `apps/agent-service/tests/life/test_proactive.py` — 验证 proactive 写完 ConversationMessage 后直接 `emit(Message.from_cm(msg))`，不经 Bridge。

**Modify:**
- `apps/agent-service/app/nodes/chat_node.py` — 删本地 `_PreSafetyResult` + `_resolve_pre_safety_for_part`；改 import；docstring "Task 8/9/10/11" → 语义阶段名。
- `apps/agent-service/app/life/proactive.py:144-148` — 改成 `import app.runtime.emit as runtime_emit; await runtime_emit.emit(Message.from_cm(msg))`。用 `runtime_emit.emit(...)` 而非 `from app.runtime.emit import emit` 是为了让 `capture_emit` fixture（patch `app.runtime.emit.emit`）天然生效。
- `apps/agent-service/app/wiring/memory.py:8` — 删头部注释里 "emit_legacy_message" 段。
- `apps/agent-service/app/main.py:31` — 删注释里 "proactive.py's Bridge calls emit_legacy_message" 段。

**Delete:**
- `apps/agent-service/app/bridges/__init__.py`
- `apps/agent-service/app/bridges/message_bridge.py`
- `apps/agent-service/app/bridges/`（空目录）
- `apps/agent-service/tests/bridges/test_message_bridge.py`
- `apps/agent-service/tests/bridges/`（空目录，若有 `__init__.py` 也一起）

---

## Test Strategy

**Existing tests that must still pass unchanged:**
- `tests/nodes/test_chat_node.py`（11 cases）— monkeypatch 的是 `cn.run_pre_safety_via_graph`（pre-safety helper 的更深层依赖），不直接 patch helper 本身。helper 拆出后 `chat_node` 模块仍 re-export `_resolve_pre_safety_for_part` + `_PreSafetyResult`（通过 `from ._chat_pre_safety import ...`），module attribute 不变，monkeypatch 不受影响。
- `tests/nodes/test_route_chat_node.py` — 不涉及 pre-safety，零影响。
- `tests/wiring/test_chat_wiring.py` — 通过 `from app.nodes.chat_node import chat_node` 验证 wiring，零影响。

**New test:** `tests/life/test_proactive.py::test_proactive_publish_emits_message_directly` 用 `capture_emit` fixture 断言 emit 收到一条 `Message`，字段从原 `ConversationMessage` lift 过来。

**Removed test:** `tests/bridges/test_message_bridge.py` 整个删除（Bridge 消失，测试自然消失）。

---

## Task 1: 拆 `_chat_pre_safety` helper 模块

**Files:**
- Create: `apps/agent-service/app/nodes/_chat_pre_safety.py`
- Modify: `apps/agent-service/app/nodes/chat_node.py:14-15, 40-73`

- [ ] **Step 1.1: 创建 `_chat_pre_safety.py`，含 `_PreSafetyResult` + `_resolve_pre_safety_for_part`**

```python
"""chat_node pre-safety segment helper.

Internal to chat_node — 调用方只有 ``app.nodes.chat_node``。下划线前缀
表示这是 chat_node 的实现细节，对外保持 chat_node 模块一个入口。

设计参见 specs/2026-05-06-dataflow-phase-5-chat-pipeline-design.md
（pre-safety BLOCK 段边界等 verdict + fail-open 语义）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _PreSafetyResult:
    blocked: bool
    content: str  # ALLOW: 原 part；BLOCK: 不用，由调用方 emit guard


async def _resolve_pre_safety_for_part(
    part: str,
    pre_task: asyncio.Task,
    guard_message: str,
    timeout: float = 5.0,
) -> _PreSafetyResult:
    """段边界等 verdict（已 done 即立刻返回，未 done 则带 timeout 等）。

    fail-open（pre_task 抛 / timeout）-> ALLOW（保持与 Phase 2 pre-safety
    设计一致的 fail-open 语义）。
    """
    if not pre_task.done():
        try:
            await asyncio.wait_for(pre_task, timeout=timeout)
        except TimeoutError:
            logger.warning("pre_safety timeout (%.1fs), fail-open", timeout)
            return _PreSafetyResult(blocked=False, content=part)
        except Exception as e:
            logger.error("pre_safety exception (fail-open): %s", e)
            return _PreSafetyResult(blocked=False, content=part)
    try:
        verdict = pre_task.result()
    except Exception as e:
        logger.error("pre_safety result raise (fail-open): %s", e)
        return _PreSafetyResult(blocked=False, content=part)
    if verdict.is_blocked:
        return _PreSafetyResult(blocked=True, content=guard_message)
    return _PreSafetyResult(blocked=False, content=part)
```

- [ ] **Step 1.2: chat_node.py 改 import + 删本地 helper**

`apps/agent-service/app/nodes/chat_node.py` 第 14-15 行（dataclass + uuid4 import 区）和第 40-73 行（本地 helper 定义）共要做两处编辑：

a) 顶部 import 区，把 `from dataclasses import dataclass` 删掉（如果只 _PreSafetyResult 用到了），加上从 `_chat_pre_safety` import：

```python
# 旧：
import asyncio
import logging
import time
from dataclasses import dataclass
from uuid import uuid4
```

替换为：

```python
import asyncio
import logging
import time
from uuid import uuid4
```

并在已有 import 段末尾（`from app.runtime.emit import emit` 下一行）追加：

```python
from app.nodes._chat_pre_safety import (
    _PreSafetyResult,
    _resolve_pre_safety_for_part,
)
```

注意：`_PreSafetyResult` 当前 chat_node 主体内并未直接使用类型注解（只通过 helper 返回值的 `result.blocked` / `result.content` 属性访问），但仍要 re-import 以保持 `chat_node` 模块的 attribute 兼容（既有测试如有需要 monkeypatch 不受影响）。

b) 删掉 chat_node.py 第 40-73 行：从 `@dataclass` 上方 `class _PreSafetyResult` 起，到 `async def _resolve_pre_safety_for_part` 函数体结束（包含 `return _PreSafetyResult(blocked=False, content=part)`）整段删除。

- [ ] **Step 1.3: 跑既有 chat_node 测试，确认零回归**

Run: `cd apps/agent-service && uv run pytest tests/nodes/test_chat_node.py -v`
Expected: 全 PASS（11 cases），无 ImportError。

- [ ] **Step 1.4: 验证行数 < 300**

Run: `wc -l apps/agent-service/app/nodes/chat_node.py`
Expected: ≤ 290（删 34 行 helper + 1 行 dataclass import = 净减 35，317-35=282 ± dataclass 单行差）

- [ ] **Step 1.5: Commit**

```bash
git add apps/agent-service/app/nodes/_chat_pre_safety.py apps/agent-service/app/nodes/chat_node.py
git commit -m "refactor(chat-dataflow): extract _chat_pre_safety helper from chat_node"
```

---

## Task 2: 清理 chat_node.py docstring 的 "Task 8/9/10/11" 用语

**Files:**
- Modify: `apps/agent-service/app/nodes/chat_node.py:138-142`

- [ ] **Step 2.1: 改 docstring 阶段名**

把第 132 行 `chat_node` docstring 的步骤列表替换为语义化阶段名。

旧（138-142）：

```python
    Phases (内部分块，不拆 node):
      1. prep: fetch + parse + gray + guard + pre_task 启动 (this task)
      2. fetch 为空 -> emit 1 段 "未找到" + return  (Task 8)
      3. resolve response_bot_name + agent_responses 行更新  (Task 9)
      4. base_payload 构造（含 lane）  (Task 9)
      5. 主循环 + 中段 emit  (Task 10)
      6. final 段 + pre-safety blocked 路径  (Task 11)
```

新：

```python
    Phases (内部分块，不拆 node):
      1. prep: fetch + parse + gray + guard + pre_task 启动
      2. fetch-empty short-circuit: emit 1 段 "未找到" + return
      3. resolve response_bot_name + agent_responses 行更新
      4. base_payload 构造（含 lane）
      5. 主循环 + 段边界 pre-safety + 中段 emit
      6. final 段 + pre-safety blocked 路径
```

- [ ] **Step 2.2: grep 验证 chat_node 内已无 "Task N" 字样**

Run: `grep -n "Task [7-9]\|Task 1[01]" apps/agent-service/app/nodes/chat_node.py`
Expected: 无输出（exit code 1）。

- [ ] **Step 2.3: 跑 chat_node 测试**

Run: `cd apps/agent-service && uv run pytest tests/nodes/test_chat_node.py tests/nodes/test_route_chat_node.py -v`
Expected: 全 PASS。

- [ ] **Step 2.4: Commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py
git commit -m "docs(chat-dataflow): rename chat_node phase markers to semantic names"
```

---

## Task 3: 新增 `tests/life/test_proactive.py`（red）

**Files:**
- Create: `apps/agent-service/tests/life/test_proactive.py`

- [ ] **Step 3.1: 先看 proactive.py 把 `publish_proactive_message` 函数签名 + 入参 + 上下文**

Run: `grep -n "^async def \|^def " apps/agent-service/app/life/proactive.py`
Expected: 列出函数定义，找到 publish 入口。我们最小测试只关注"调用 publish_proactive_message 后 emit 被调用一次，参数是 Message 实例"。

- [ ] **Step 3.2: 写测试（red — 当前 proactive.py 还在调 emit_legacy_message，我们直接断言新行为，先红后绿）**

注意：proactive.py 当前调 `emit_legacy_message(msg)`，里面也是 `await emit(Message.from_cm(cm))`，所以 `capture_emit` fixture 在 Task 4 改造之前其实也能捕到 Message。这条测试 Task 3 写完直接跑就 PASS（不严格 red），但 Task 4 改造后行为不变也 PASS——是 invariant 测试。仍然 TDD 流程上保留"先写测试"。

`apps/agent-service/tests/life/test_proactive.py`：

```python
"""Phase 5b — proactive 写完 ConversationMessage 后直接 emit Message（不经 Bridge）。

invariant 测试：5b 之前 proactive 经 ``emit_legacy_message(msg)``，
5b 之后直连 ``await emit(Message.from_cm(msg))``，对 capture_emit
fixture 的可观察行为相同——一条 ``Message`` 被 emit。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.domain.message import Message


@pytest.mark.asyncio
async def test_proactive_publish_emits_message_directly(capture_emit, monkeypatch):
    """publish_proactive_message → DB write → emit(Message)。"""
    from app.life import proactive as pro

    # Stub DB session — 我们关心 emit 调用，不关心 DB 落盘。
    class _FakeSession:
        def add(self, _msg):
            pass

    class _FakeSessionCtx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(pro, "get_session", lambda: _FakeSessionCtx())

    # Stub mq.publish — 不关心 RabbitMQ 实际发布。
    fake_publish = AsyncMock()
    monkeypatch.setattr(pro.mq, "publish", fake_publish)

    await pro.publish_proactive_message(
        chat_id="c1",
        bot_name="赤尾",
        content="hi",
        target_lark_id=None,
        root_message_id=None,
    )

    # capture_emit fixture patched app.runtime.emit.emit → proactive 的
    # ``import app.runtime.emit as runtime_emit; await runtime_emit.emit(...)``
    # 走 attribute lookup，每次都拿到 fake，被记录到 ``capture_emit`` list。
    assert len(capture_emit) == 1
    emitted = capture_emit[0]
    assert isinstance(emitted, Message)
    assert emitted.chat_id == "c1"
    assert emitted.bot_name == "赤尾"
    assert emitted.content == "hi"
    assert emitted.role == "user"
    assert emitted.message_type == "proactive_trigger"
```

- [ ] **Step 3.3: 跑测试 — 当前应 FAIL**

Run: `cd apps/agent-service && uv run pytest tests/life/test_proactive.py -v`
Expected: FAIL — 因为当前 proactive.py 用 `from app.bridges.message_bridge import emit_legacy_message`，里面的 `emit` import 是 emit_mod.emit 的 binding（非 attribute lookup），capture_emit fixture monkeypatch `emit_mod.emit` 不会改 message_bridge 模块内已 bind 的 emit 引用 → emit 不会被 capture，`assert len(capture_emit) == 1` 失败。

如果出乎意料 PASS，说明 fixture 设计能透传，那 Task 3 阶段就 green，仍按 plan 走 Task 4 切换 import 路径。

- [ ] **Step 3.4: Commit（red 阶段也 commit，方便回退）**

```bash
git add apps/agent-service/tests/life/test_proactive.py
git commit -m "test(chat-dataflow): proactive emits Message directly (red)"
```

---

## Task 4: proactive.py 直接 `emit(Message.from_cm(msg))`（green）

**Files:**
- Modify: `apps/agent-service/app/life/proactive.py:142-148`

- [ ] **Step 4.1: 替换 import + 调用**

旧：

```python
    # get_session() commits on block exit; emit AFTER commit so downstream
    # consumers querying pg will see the row.
    from app.bridges.message_bridge import (
        emit_legacy_message,  # local import to avoid boot cycles
    )

    await emit_legacy_message(msg)
```

新：

```python
    # get_session() commits on block exit; emit AFTER commit so downstream
    # consumers querying pg will see the row.
    import app.runtime.emit as runtime_emit  # local import to avoid boot cycles
    from app.domain.message import Message

    await runtime_emit.emit(Message.from_cm(msg))
```

设计选择：用 `import app.runtime.emit as runtime_emit` + `runtime_emit.emit(...)` 而非 `from app.runtime.emit import emit` 直 emit。两者运行时等价，但前者每次调用都走 module attribute lookup，让 `tests/conftest.py:capture_emit` fixture（monkeypatch `emit_mod.emit`）天然可见——避免在 fixture 上加针对 proactive 模块的特例 patch。`Message` 也走 local import，统一在 commit 之后才生效，跟原 Bridge 局部 import 风格一致。

- [ ] **Step 4.2: 跑 Task 3 那条测试 — 现在应 PASS**

Run: `cd apps/agent-service && uv run pytest tests/life/test_proactive.py -v`
Expected: PASS。

- [ ] **Step 4.3: 跑全 chat / life 测试，确认零回归**

Run: `cd apps/agent-service && uv run pytest tests/life tests/nodes tests/wiring -v`
Expected: 全 PASS。

- [ ] **Step 4.4: Commit**

```bash
git add apps/agent-service/app/life/proactive.py
git commit -m "feat(chat-dataflow): proactive emits Message directly (drop Bridge)"
```

---

## Task 5: 删 `app/bridges/` + `tests/bridges/`

**Files:**
- Delete: `apps/agent-service/app/bridges/__init__.py`
- Delete: `apps/agent-service/app/bridges/message_bridge.py`
- Delete: `apps/agent-service/tests/bridges/test_message_bridge.py`
- Delete: `apps/agent-service/app/bridges/`（空目录）
- Delete: `apps/agent-service/tests/bridges/`（含 `__init__.py` 若存在）

- [ ] **Step 5.1: 确认没有 import 还引用 bridges**

Run: `grep -rn "from app.bridges\|import app.bridges\|app\.bridges" apps/agent-service/`
Expected: 仅 plan/spec 文档命中，app/ 和 tests/ 下零命中。

- [ ] **Step 5.2: 用 git rm 删文件 + 目录**

```bash
git rm apps/agent-service/app/bridges/__init__.py
git rm apps/agent-service/app/bridges/message_bridge.py
git rm apps/agent-service/tests/bridges/test_message_bridge.py
# 若 tests/bridges/__init__.py 存在，一并删；空目录 git 自动忽略
[ -f apps/agent-service/tests/bridges/__init__.py ] && git rm apps/agent-service/tests/bridges/__init__.py
rmdir apps/agent-service/app/bridges apps/agent-service/tests/bridges 2>/dev/null || true
```

- [ ] **Step 5.3: 跑全测试套件**

Run: `cd apps/agent-service && uv run pytest -x -q`
Expected: 全 PASS（test_message_bridge.py 已不存在，不会报 not collected）。

- [ ] **Step 5.4: Commit**

```bash
git add -A apps/agent-service/app/bridges apps/agent-service/tests/bridges 2>/dev/null
git commit -m "chore(chat-dataflow): remove app/bridges (Bridge phase ended)"
```

---

## Task 6: 清理 wiring/memory.py + main.py 注释里的 Bridge 残留

**Files:**
- Modify: `apps/agent-service/app/wiring/memory.py:8`（头部 docstring）
- Modify: `apps/agent-service/app/main.py:31`（boot 注释）

- [ ] **Step 6.1: 看现状两处注释**

Run: `grep -nB2 -A2 "emit_legacy_message" apps/agent-service/app/wiring/memory.py apps/agent-service/app/main.py`

记下原文行 — 例如 `wiring/memory.py:8` 是 `  * emit_legacy_message(cm) (inside proactive.py and other Python-side ...)` 这行。把"通过 Bridge 写入 Message"的描述改成"通过 proactive.py 直接 emit Message"，或直接删除已过期的项目说明。

- [ ] **Step 6.2: 编辑**

`wiring/memory.py:8` 头注释里"emit_legacy_message"那条 bullet 整行删除（如果上下文还有 enumerate 改成 enumerate-1）；如果整段说明已无意义，整段段落都可以删——以可读性为准，不留指向已删模块的死指针。

`main.py:31`：把"proactive.py's Bridge calls emit_legacy_message() which dispatches"那段改成"proactive.py emits Message directly via runtime emit"，或直接删除（boot 注释作用已经过时）。

具体由 worker 在执行时根据上下文做最小可读化修改，目标：grep `emit_legacy_message` 在 `apps/agent-service/app/` 下 0 命中。

- [ ] **Step 6.3: grep 验证**

Run: `grep -rn "emit_legacy_message\|message_bridge" apps/agent-service/app/`
Expected: 0 命中。

Run: `grep -rn "emit_legacy_message\|message_bridge" apps/agent-service/tests/`
Expected: 0 命中（test_message_bridge.py 已 Task 5 删除）。

- [ ] **Step 6.4: Commit**

```bash
git add apps/agent-service/app/wiring/memory.py apps/agent-service/app/main.py
git commit -m "docs(chat-dataflow): drop emit_legacy_message references in boot comments"
```

---

## Task 7: 全局 grep 验收 + 全量测试

**Files:** 无

- [ ] **Step 7.1: spec 验收命令（来自 spec line 518/525/590）**

```bash
grep -rn "message_bridge\|emit_legacy_message" apps/agent-service/
```
Expected: 0 命中。

- [ ] **Step 7.2: 验证 chat_node.py < 300 行**

```bash
wc -l apps/agent-service/app/nodes/chat_node.py
```
Expected: ≤ 290。

- [ ] **Step 7.3: 验证 chat_node.py 已无 "Task 7-11" 残留**

```bash
grep -n "Task [7-9]\|Task 1[01]" apps/agent-service/app/nodes/chat_node.py
```
Expected: 0 命中。

- [ ] **Step 7.4: 全量测试**

```bash
cd apps/agent-service && uv run pytest -q
```
Expected: 全 PASS（no errors, no warnings about missing collected items）。

- [ ] **Step 7.5: ruff / lint（按项目实际命令）**

```bash
cd apps/agent-service && uv run ruff check app/ tests/
```
Expected: 0 issues。

如果项目还有 mypy / pyright，按现有 CI 配置跑一次。

---

## Task 8: 推送分支 + 创建 PR

**Files:** 无

- [ ] **Step 8.1: 看 commit 日志**

```bash
git log main..HEAD --oneline
```
Expected: 6 个 commit（Task 1-6 各一个），按顺序：
1. refactor(chat-dataflow): extract _chat_pre_safety helper from chat_node
2. docs(chat-dataflow): rename chat_node phase markers to semantic names
3. test(chat-dataflow): proactive emits Message directly (red)
4. feat(chat-dataflow): proactive emits Message directly (drop Bridge)
5. chore(chat-dataflow): remove app/bridges (Bridge phase ended)
6. docs(chat-dataflow): drop emit_legacy_message references in boot comments

- [ ] **Step 8.2: push**

```bash
git push -u origin refactor/flow-parse-5b
```

- [ ] **Step 8.3: 创建 PR（English title + body，禁止中文 / emoji 字符）**

```bash
ghc pr create --title "Phase 5b: drop app/bridges + extract _chat_pre_safety helper" --body "$(cat <<'EOF'
## Summary

- Drop `app/bridges/` package (Phase 1-5a transitional Bridge layer)
- `app/life/proactive.py` now emits `Message` directly via runtime emit
- Extract `_PreSafetyResult` + `_resolve_pre_safety_for_part` from `chat_node.py` into `app/nodes/_chat_pre_safety.py`; main file drops below 300 lines
- Rename `chat_node` docstring phase markers from "Task 8/9/10/11" to semantic names
- Drop stale `emit_legacy_message` references in `wiring/memory.py` and `main.py` comments

## Test plan

- [ ] `pytest apps/agent-service` all green
- [ ] `grep -rn "message_bridge\|emit_legacy_message" apps/agent-service/` returns 0
- [ ] `wc -l apps/agent-service/app/nodes/chat_node.py` <= 290
- [ ] `grep -n "Task [7-9]\|Task 1[01]" apps/agent-service/app/nodes/chat_node.py` returns 0
- [ ] Deploy to dev lane `feat-flow-parse-5b`, send proactive trigger, verify Message lands in vectorize queue
- [ ] Smoke chat reply path (non-proactive) still works

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 8.4: 把 PR URL 报告给用户**

---

## Self-Review Checklist

- [x] **Spec coverage**：spec line 414-423（proactive 改造 + 删 bridges）→ Task 4-5；line 516（test_proactive 用 capture_emit）→ Task 3；line 590（grep 验收）→ Task 7。5a memory followup 三条全覆盖。
- [x] **Placeholder scan**：每步含具体代码 / 命令 / 期望输出，没有"添加适当错误处理"等泛指。
- [x] **Type consistency**：`_PreSafetyResult` 字段（`blocked`、`content`）+ `_resolve_pre_safety_for_part` 签名 全程一致。`runtime_emit.emit(Message.from_cm(msg))` 在 Task 3 测试断言和 Task 4 实现里都用同一形式。
- [x] **Risks**：
  - 主要风险点是 Task 4 改 proactive.py 的 import 风格（`from X import Y` → `import X as Y`），如果别处也有 monkeypatch 直接 patch `app.life.proactive.emit_legacy_message`，会失效——已 grep 过，整个仓库没有。
  - chat_node 既有测试 monkeypatch 的 helper 名都是 `run_pre_safety_via_graph` 等更深层依赖，拆 helper 不影响。

---

## Out of Scope

显式不在 5b 范围：
- `app/runtime/debounce.py:144` 的 "Task 8" 注释（Phase 3 plan 残留，不是 chat-dataflow 链）
- Phase 5 spec 文档自身的 "5b 后" 描述（spec 是历史记录，shipped 后冻结）
- agent tool 副作用 commit_abstract_memory 进 wire（Phase 6+）
- "chat 部分段后中断、新 Pod 不续传"（需 chat_node 状态持久化，独立 epic）
