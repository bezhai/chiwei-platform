# Dataflow Phase 5a — Chat 主 Pipeline 进 Graph 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现 `workers/chat_consumer.py` + `chat/pipeline.py:stream_chat` 链路改造成 dataflow graph：mq(chat_request) → `route_chat_node` → ChatRequest → `chat_node` → ChatResponseSegment → Sink.mq("chat_response")。同时搭车删除 `runtime/stream.py` 这个不再需要的 type marker。

**Architecture:** 一个大 `chat_node`（per persona）+ 一个 `route_chat_node`（fan-out）。两个节点 + 三个 Data 类 + 三条 wire 声明。chat_consumer.py 整文件删除，pipeline.py 被掏空（helper 搬到 nodes/chat_node.py）。

**Tech Stack:** Python 3.11 + asyncio + dataclass-style Data + 项目自有 dataflow runtime（Phase 0 落地）。测试用 pytest + pytest-asyncio。包管理用 `uv`。

**Spec:** [docs/superpowers/specs/2026-05-06-dataflow-phase-5-chat-pipeline-design.md](../specs/2026-05-06-dataflow-phase-5-chat-pipeline-design.md)（v4 + clean-up commit `4924c85`）

---

## File Structure

### 新建

| 文件 | 职责 | 行数预估 |
|---|---|---|
| `apps/agent-service/app/domain/chat_dataflow.py` | ChatTrigger / ChatRequest / ChatResponseSegment Data 类 | ~120 |
| `apps/agent-service/app/nodes/chat_node.py` | route_chat_node + chat_node + 内部 helper（_resolve_pre_safety_for_part / _build_and_stream 搬过来） | ~280 |
| `apps/agent-service/app/wiring/chat.py` | 3 条 wire 声明 | ~25 |
| `apps/agent-service/tests/domain/test_chat_dataflow.py` | Data 类字段 + Key + transient 测试 | ~80 |
| `apps/agent-service/tests/nodes/test_route_chat_node.py` | route_chat_node 单元测试 | ~150 |
| `apps/agent-service/tests/nodes/test_chat_node.py` | chat_node 单元测试 | ~250 |
| `apps/agent-service/tests/wiring/test_chat_wiring.py` | wiring 编译 + 表存在断言 | ~60 |
| `apps/agent-service/tests/dataflow/__init__.py` | 新建目录占位 | 0 |
| `apps/agent-service/tests/dataflow/test_chat_dedup.py` | dedup 层级测试 | ~100 |

### 修改

| 文件 | 改动 |
|---|---|
| `apps/agent-service/app/wiring/__init__.py` | 加 `chat` import |
| `apps/agent-service/app/main.py` | 删 `start_chat_consumer()` 调用 |
| `apps/agent-service/app/chat/__init__.py` | 删 `from app.chat.pipeline import stream_chat` |
| `apps/agent-service/app/runtime/node.py` | 删 `is_stream` import + Stream 校验代码段 + 改文档注释 |
| `apps/agent-service/tests/conftest.py` | 加 `capture_emit` fixture（如不存在） |
| `docs/superpowers/specs/2026-04-21-agent-dataflow-abstraction-design.md` | 加 Stream[T] errata 段 |

### 删除

| 文件 | 原因 |
|---|---|
| `apps/agent-service/app/workers/chat_consumer.py` | 整体替代 |
| `apps/agent-service/app/chat/pipeline.py` | helper 搬走，文件整体可删 |
| `apps/agent-service/app/runtime/stream.py` | 不再使用的 type marker |

---

## Task 路线图

| Task | 主题 | 依赖 |
|---|---|---|
| 1 | 三个 Data 类 + 测试 | — |
| 2 | 占位 node + wiring + compile_graph 测试 | 1 |
| 3 | 删 Stream[T] runtime + node.py 校验 + spec errata | — |
| 4 | route_chat_node: message_id None 校验 | 2 |
| 5 | route_chat_node: redelivered 短路 | 4 |
| 6 | route_chat_node: MessageRouter + fan-out | 5 |
| 7 | chat_node: prep 块 | 2 |
| 8 | chat_node: 找不到消息早返回 | 7 |
| 9 | chat_node: resolve bot_name + base_payload | 7 |
| 10 | chat_node: 主流 + 中段 emit + lane | 9 |
| 11 | chat_node: final 段 + pre-safety blocked 路径 | 10 |
| 12 | 删 chat_consumer.py + 主程序入口清理 | 6, 11 |
| 13 | dedup 测试 + capture_emit fixture + grep 自检 | 12 |
| 14 | 泳道部署 + e2e（用户执行） | 13 |

---

## Task 1: 三个 Data 类 + 测试

**Files:**
- Create: `apps/agent-service/app/domain/chat_dataflow.py`
- Create: `apps/agent-service/tests/domain/test_chat_dataflow.py`

- [ ] **Step 1.1: 写失败测试**

Create `apps/agent-service/tests/domain/test_chat_dataflow.py`:

```python
"""ChatTrigger / ChatRequest / ChatResponseSegment Data 类字段合约。"""
from app.runtime.data import key_fields


def test_chat_trigger_has_message_id_key_and_is_transient():
    from app.domain.chat_dataflow import ChatTrigger
    assert "message_id" in key_fields(ChatTrigger)
    assert ChatTrigger.Meta.transient is True


def test_chat_trigger_optional_fields_default_none():
    from app.domain.chat_dataflow import ChatTrigger
    t = ChatTrigger(message_id="m1")
    assert t.session_id is None
    assert t.chat_id is None
    assert t.is_p2p is False
    assert t.user_id is None
    assert t.lane is None
    assert t.is_proactive is False
    assert t.bot_name is None
    assert t.mentions == []
    assert t.enqueued_at is None


def test_chat_trigger_message_id_can_be_none_for_validation_resilience():
    """lark-server 偶尔不带 message_id；Data 反序列化要能成功。"""
    from app.domain.chat_dataflow import ChatTrigger
    t = ChatTrigger()
    assert t.message_id is None


def test_chat_request_has_message_id_persona_id_keys_not_transient():
    from app.domain.chat_dataflow import ChatRequest
    keys = key_fields(ChatRequest)
    assert "message_id" in keys
    assert "persona_id" in keys
    assert getattr(ChatRequest.Meta, "transient", False) is False  # 默认即可


def test_chat_request_has_lane_field():
    from app.domain.chat_dataflow import ChatRequest
    r = ChatRequest(message_id="m1", persona_id="p1")
    assert r.lane is None  # 默认 None
    r2 = ChatRequest(message_id="m1", persona_id="p1", lane="dev")
    assert r2.lane == "dev"


def test_chat_response_segment_dedup_keys_and_lane():
    from app.domain.chat_dataflow import ChatResponseSegment
    keys = key_fields(ChatResponseSegment)
    assert "message_id" in keys
    assert "persona_id" in keys
    assert "part_index" in keys
    seg = ChatResponseSegment(message_id="m1", persona_id="p1", part_index=0)
    assert seg.lane is None
    assert seg.is_last is False
    assert seg.status == "success"
    assert seg.content == ""


def test_chat_response_segment_is_transient():
    from app.domain.chat_dataflow import ChatResponseSegment
    assert ChatResponseSegment.Meta.transient is True
```

- [ ] **Step 1.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/domain/test_chat_dataflow.py -v
```

Expected: ImportError / ModuleNotFoundError，因为 `app.domain.chat_dataflow` 不存在。

- [ ] **Step 1.3: 写最小实现**

Create `apps/agent-service/app/domain/chat_dataflow.py`:

```python
"""Phase 5a chat pipeline Data 类。

  - ChatTrigger: mq(chat_request) 入口的原始 body（lark-server 发来）
  - ChatRequest: 经 route_chat_node fan-out 后 per-persona 的请求
  - ChatResponseSegment: chat_node 输出的段，最终 publish 到 mq(chat_response)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from app.runtime.data import Data, Key


@dataclass
class ChatTrigger(Data):
    """mq(chat_request) 入口的原始 body（lark-server publish）。

    所有字段均设默认值，匹配 chat_consumer.handle_chat_request 现行
    body.get(..., default) 行为；lark-server 偶尔不带 is_proactive /
    user_id / bot_name 等，Data 反序列化必须不挂。
    """

    message_id: Annotated[str | None, Key] = None  # 至少一个 Key（runtime 强约束）
    session_id: str | None = None
    chat_id: str | None = None
    is_p2p: bool = False
    root_id: str | None = None
    user_id: str | None = None
    lane: str | None = None
    is_proactive: bool = False
    bot_name: str | None = None
    mentions: list[str] = field(default_factory=list)
    enqueued_at: int | None = None

    class Meta:
        transient = True  # source.mq 入口不调 insert_idempotent，建表无意义


@dataclass
class ChatRequest(Data):
    """每个 persona 一个，由 route_chat_node fan-out 出来。

    走 in-graph durable 投递；durable consumer handler 在 chat_node 入口
    insert_idempotent 拦下 (message_id, persona_id) 重投。
    """

    message_id: Annotated[str, Key] = ""
    persona_id: Annotated[str, Key] = ""
    session_id: str | None = None
    chat_id: str | None = None
    is_p2p: bool = False
    root_id: str | None = None
    user_id: str | None = None
    is_proactive: bool = False
    bot_name: str | None = None
    lane: str | None = None
    enqueued_at: int | None = None

    # 默认 transient=False（runtime 自动建 data_chat_request 表）


@dataclass
class ChatResponseSegment(Data):
    """chat_node 输出的段，sink → mq(chat_response) → lark-server chat-response-worker。

    sink dispatch 不注入 header lane，body 必须显式带 lane 字段（lark-server
    chat-response-worker 现行读 payload.lane）。
    """

    message_id: Annotated[str, Key] = ""
    persona_id: Annotated[str, Key] = ""
    part_index: Annotated[int, Key] = 0
    session_id: str | None = None
    chat_id: str | None = None
    is_p2p: bool = False
    root_id: str | None = None
    user_id: str | None = None
    is_proactive: bool = False
    bot_name: str | None = None
    lane: str | None = None
    content: str = ""
    status: str = "success"
    is_last: bool = False
    full_content: str | None = None
    published_at: int | None = None

    class Meta:
        transient = True  # sink 不需要持久化表
```

- [ ] **Step 1.4: 跑测试，确认通过**

```bash
uv run pytest apps/agent-service/tests/domain/test_chat_dataflow.py -v
```

Expected: 7 passed.

- [ ] **Step 1.5: commit**

```bash
git add apps/agent-service/app/domain/chat_dataflow.py apps/agent-service/tests/domain/test_chat_dataflow.py
git commit -m "feat(chat-dataflow): add ChatTrigger / ChatRequest / ChatResponseSegment Data classes

Phase 5a Task 1: data classes for the chat dataflow graph.
- ChatTrigger transient=True (no insert_idempotent at source.mq).
- ChatRequest transient=False, (message_id, persona_id) joint Key —
  runtime auto-creates data_chat_request and dedups in-graph durable
  redelivery.
- ChatResponseSegment carries lane in body since sink dispatch does not
  inject header lane."
```

---

## Task 2: 占位 node + wiring + compile_graph 测试

**Files:**
- Create: `apps/agent-service/app/nodes/chat_node.py`
- Create: `apps/agent-service/app/wiring/chat.py`
- Modify: `apps/agent-service/app/wiring/__init__.py`
- Create: `apps/agent-service/tests/wiring/test_chat_wiring.py`

- [ ] **Step 2.1: 写失败测试**

Create `apps/agent-service/tests/wiring/test_chat_wiring.py`:

```python
"""Phase 5a chat wiring compile + migrator 表存在断言。"""
import pytest

from app.runtime.graph import clear_wiring, compile_graph


@pytest.fixture
def reset_wiring():
    clear_wiring()
    yield
    clear_wiring()


def test_chat_wiring_compiles(reset_wiring):
    """加载 chat wiring 后 compile_graph 不抛。"""
    import app.wiring.chat  # noqa: F401, 触发 wire() 副作用
    # 同时把其它依赖的 wiring 也 import（避免 isolated wire 编译失败）
    import app.wiring.safety  # noqa: F401
    g = compile_graph()
    assert g is not None


def test_chat_wiring_has_three_wires(reset_wiring):
    """ChatTrigger / ChatRequest / ChatResponseSegment 三条 wire。"""
    import app.wiring.chat  # noqa: F401
    from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
    from app.runtime.wire import _registered_wires  # 内部 API；没有则换 graph compile 输出

    types = {w.data_type for w in _registered_wires()}
    assert ChatTrigger in types
    assert ChatRequest in types
    assert ChatResponseSegment in types


def test_chat_request_table_in_migrator(reset_wiring):
    """ChatRequest transient=False -> migrator 应建表 data_chat_request。"""
    import app.wiring.chat  # noqa: F401
    from app.runtime.migrator import collect_data_classes
    from app.domain.chat_dataflow import ChatRequest, ChatTrigger

    classes = collect_data_classes()
    assert ChatRequest in classes
    assert ChatTrigger not in classes  # transient=True 不建表
```

> **note**: `_registered_wires` / `collect_data_classes` 是内部 API。如果它们的实际导出名不一样，按 runtime 现有名字调整。如果 runtime 没暴露遍历 wire 的方法，改成直接 catch `compile_graph()` 的 wire 数量（look at `wire(...)` 的全局注册器）。

- [ ] **Step 2.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/wiring/test_chat_wiring.py -v
```

Expected: 3 failed（chat_node / wiring/chat.py 不存在）。

- [ ] **Step 2.3: 写占位 chat_node + route_chat_node**

Create `apps/agent-service/app/nodes/chat_node.py`:

```python
"""Phase 5a chat 主 pipeline 节点。

  route_chat_node:  ChatTrigger -> N × emit(ChatRequest)
  chat_node:        ChatRequest -> N × emit(ChatResponseSegment)

本文件是占位骨架，业务逻辑见后续 task。
"""
from __future__ import annotations

import logging

from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.runtime.node import node

logger = logging.getLogger(__name__)


@node
async def route_chat_node(t: ChatTrigger) -> None:
    """ChatTrigger -> ChatRequest fan-out（占位）。"""
    raise NotImplementedError("route_chat_node body added in later tasks")


@node
async def chat_node(req: ChatRequest) -> None:
    """ChatRequest -> ChatResponseSegment 生成（占位）。"""
    raise NotImplementedError("chat_node body added in later tasks")
```

Create `apps/agent-service/app/wiring/chat.py`:

```python
"""Phase 5a chat 主 pipeline wiring.

  mq(chat_request)
       ─[wire ChatTrigger, NO .durable()]→  route_chat_node
                                              │
                                              ↓ N × emit(ChatRequest)
       ─[wire ChatRequest, .durable()]→  chat_node
                                              │
                                              ↓ N × emit(ChatResponseSegment)
       ─[wire ChatResponseSegment, NO .durable()]→  Sink.mq("chat_response")
                                              ↓
                                  lark-server / chat-response-worker → 飞书
"""
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.nodes.chat_node import chat_node, route_chat_node
from app.runtime import Sink, Source, wire

wire(ChatTrigger).from_(Source.mq("chat_request")).to(route_chat_node)
wire(ChatRequest).to(chat_node).durable()
wire(ChatResponseSegment).to(Sink.mq("chat_response"))
```

Modify `apps/agent-service/app/wiring/__init__.py`:

```python
"""Aggregator: import every wiring module so wire() 副作用 register 全图。"""

from app.wiring import (  # noqa: F401
    chat,
    life_dataflow,
    memory,
    memory_triggers,
    memory_vectorize,
    safety,
)
```

- [ ] **Step 2.4: 跑测试**

```bash
uv run pytest apps/agent-service/tests/wiring/test_chat_wiring.py -v
```

Expected: 3 passed.

- [ ] **Step 2.5: 跑全测试套确保没回归**

```bash
uv run pytest apps/agent-service/tests/ -x --timeout=30
```

Expected: 全部 pass（runtime/wiring/migrator 集成测试也应当过）。

- [ ] **Step 2.6: commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py apps/agent-service/app/wiring/chat.py apps/agent-service/app/wiring/__init__.py apps/agent-service/tests/wiring/test_chat_wiring.py
git commit -m "feat(chat-dataflow): wiring skeleton + placeholder nodes

Phase 5a Task 2: chat wiring compiles end-to-end with placeholder
nodes. compile_graph() succeeds; migrator picks up data_chat_request
table (ChatRequest transient=False) and skips ChatTrigger /
ChatResponseSegment (transient=True)."
```

---

## Task 3: 删 Stream[T] runtime + node.py 校验 + spec errata

**Files:**
- Delete: `apps/agent-service/app/runtime/stream.py`
- Modify: `apps/agent-service/app/runtime/node.py`
- Modify: `docs/superpowers/specs/2026-04-21-agent-dataflow-abstraction-design.md`

- [ ] **Step 3.1: 写失败测试（确认 Stream import 不再可用 + node 装饰器接受 Annotated 仅 Data）**

Create `apps/agent-service/tests/runtime/test_no_stream_marker.py`:

```python
"""Phase 5a 搭车：删 runtime/stream.py + node.py 里的 is_stream 校验。"""
import pytest


def test_runtime_stream_module_does_not_exist():
    with pytest.raises(ModuleNotFoundError):
        import app.runtime.stream  # noqa: F401


def test_node_decorator_does_not_import_is_stream():
    """node.py 不应继续 import is_stream。"""
    import inspect

    from app.runtime import node as node_mod

    src = inspect.getsource(node_mod)
    assert "is_stream" not in src, (
        "node.py 仍在引用 is_stream；按 Phase 5a 应同步移除"
    )
    assert "from app.runtime.stream" not in src
```

- [ ] **Step 3.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/runtime/test_no_stream_marker.py -v
```

Expected: 2 failed（stream.py 仍存在 + node.py 仍 import is_stream）。

- [ ] **Step 3.3: 删 runtime/stream.py**

```bash
rm apps/agent-service/app/runtime/stream.py
```

- [ ] **Step 3.4: 改 runtime/node.py**

Modify `apps/agent-service/app/runtime/node.py` —— 删除 3 处：

1. import 行（约 line 35）：删 `from app.runtime.stream import is_stream`
2. 参数校验段（约 line 79-83）—— 整段删：

```python
        if is_stream(t):
            raise TypeError(
                f"{fn.__name__}.{name}: Stream[X] is not supported; the "
                f"runtime has no async-iteration dispatch yet"
            )
```

3. 返回值校验段（约 line 92-97）—— 整段删：

```python
        if is_stream(unwrapped):
            raise TypeError(
                f"{fn.__name__} returns Stream[X] which is not supported; "
                f"the runtime wrapper only auto-emits a single Data instance"
            )
```

4. 修改文件头文档字符串（约 line 19-23）—— 删除：

```
``Stream[T]`` parameters / returns are intentionally rejected: the
runtime wrapper only auto-emits a single ``Data`` instance and has no
async-iteration dispatch. The type marker exists in ``app.runtime.stream``
for future use but is not part of the public API today; using it in a
``@node`` signature raises ``TypeError`` at decorate time.
```

5. 同时把第 14-16 行的 "spec forbids business code from calling emit / mq.publish to the next hop manually" 改为：

```
Behavior: the decorator wraps ``fn`` so that a returned ``Data`` is
automatically emitted into the graph via ``runtime.emit.emit``. ``None``
returns are skipped. Multi-output cases (fan-out, streaming segment
emission per chunk) are expressed by calling ``await emit(...)``
directly inside the @node body — this is in active use since Phase 4
(see ``nodes/life_dataflow._fan_out_per_persona``) and is the canonical
way to handle "one call produces multiple values". The wrapper still
returns the value to its caller so unit tests can assert on it.
```

- [ ] **Step 3.5: 跑专项测试**

```bash
uv run pytest apps/agent-service/tests/runtime/test_no_stream_marker.py -v
```

Expected: 2 passed.

- [ ] **Step 3.6: 跑全套测试**

```bash
uv run pytest apps/agent-service/tests/ -x --timeout=30
```

Expected: 全部 pass。如果有别处引用 `app.runtime.stream`（比如旧测试），grep 出来逐处删。

```bash
grep -rn "from app.runtime.stream\|app\\.runtime\\.stream" apps/agent-service/ --include="*.py"
```

Expected: 无输出。

- [ ] **Step 3.7: 加源 spec errata**

Modify `docs/superpowers/specs/2026-04-21-agent-dataflow-abstraction-design.md` ——
找到 "Stream[T] — 流式作为一等公民" 段（line 105 附近），在末尾加：

```markdown
> **Errata（Phase 5a，2026-05-06）**: `Stream[T]` 经 Phase 1-4 实践证伪。
> "一调用产多值"场景由 `@node` 内部多次 `await emit(...)` 表达即可
> （`nodes/life_dataflow._fan_out_per_persona` 自 Phase 4 起大规模在用；
> chat 的段输出在 Phase 5a 同样走这个模式）。Phase 5a 落地时删除
> `runtime/stream.py` 与 `node.py` 的 `Stream` 校验。
```

- [ ] **Step 3.8: commit**

```bash
git add apps/agent-service/app/runtime/node.py docs/superpowers/specs/2026-04-21-agent-dataflow-abstraction-design.md apps/agent-service/tests/runtime/test_no_stream_marker.py
git rm apps/agent-service/app/runtime/stream.py
git commit -m "refactor(runtime): drop Stream[T] type marker + node.py reject path

Stream[T] has no users since Phase 0 — every fan-out / multi-output
case in Phase 1-4 uses '@node body calls emit() multiple times'
(life_dataflow._fan_out_per_persona is the canonical example since
Phase 4). Delete runtime/stream.py and the is_stream check in
runtime/node.py. Source design doc gets an errata note."
```

---

## Task 4: route_chat_node — message_id None 校验

**Files:**
- Modify: `apps/agent-service/app/nodes/chat_node.py`
- Create: `apps/agent-service/tests/nodes/test_route_chat_node.py`

- [ ] **Step 4.1: 写失败测试**

Create `apps/agent-service/tests/nodes/test_route_chat_node.py`:

```python
"""route_chat_node 单元测试（Task 4-6 累积）。"""
import pytest

from app.domain.chat_dataflow import ChatTrigger


@pytest.mark.asyncio
async def test_route_chat_node_raises_on_missing_message_id():
    """缺 message_id -> raise，不静默 fan-out 空 ChatRequest。"""
    from app.nodes.chat_node import route_chat_node

    t = ChatTrigger()  # 全部默认值，message_id=None
    with pytest.raises((ValueError, AssertionError)):
        await route_chat_node(t)
```

- [ ] **Step 4.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/nodes/test_route_chat_node.py -v
```

Expected: 1 failed（NotImplementedError 而不是 ValueError）。

- [ ] **Step 4.3: 改 route_chat_node**

Modify `apps/agent-service/app/nodes/chat_node.py` route_chat_node：

```python
@node
async def route_chat_node(t: ChatTrigger) -> None:
    """ChatTrigger -> ChatRequest fan-out。"""
    if t.message_id is None:
        raise ValueError(
            "ChatTrigger.message_id is None; cannot fan out ChatRequest"
        )
    # 后续 task 加 redelivered + router + emit
```

- [ ] **Step 4.4: 跑测试**

```bash
uv run pytest apps/agent-service/tests/nodes/test_route_chat_node.py -v
```

Expected: 1 passed.

- [ ] **Step 4.5: commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py apps/agent-service/tests/nodes/test_route_chat_node.py
git commit -m "feat(chat-dataflow): route_chat_node guards missing message_id"
```

---

## Task 5: route_chat_node — redelivered 短路

**Files:**
- Modify: `apps/agent-service/app/nodes/chat_node.py`
- Modify: `apps/agent-service/tests/nodes/test_route_chat_node.py`

- [ ] **Step 5.1: 写失败测试**

Append to `apps/agent-service/tests/nodes/test_route_chat_node.py`:

```python
@pytest.mark.asyncio
async def test_route_chat_node_short_circuits_when_completed(monkeypatch):
    """is_chat_request_completed 返 True -> 直接 return，不 emit。"""
    from app.nodes import chat_node as chat_node_mod
    from app.runtime.emit import reset_emit_runtime

    reset_emit_runtime()
    seen = []
    captured_kwargs = {}

    async def fake_completed(session, session_id, *, is_proactive=False):
        captured_kwargs["session_id"] = session_id
        captured_kwargs["is_proactive"] = is_proactive
        return True

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    # 兜底：emit 不应被调用
    async def fake_emit(*a, **k):
        seen.append((a, k))
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)

    t = ChatTrigger(message_id="m1", session_id="s1", is_proactive=True)
    await chat_node_mod.route_chat_node(t)

    assert captured_kwargs == {"session_id": "s1", "is_proactive": True}
    assert seen == []  # 被 short-circuit


@pytest.mark.asyncio
async def test_route_chat_node_runs_router_when_not_completed(monkeypatch):
    """is_chat_request_completed 返 False -> 继续往下跑（验证至少不抛）。"""
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(session, session_id, *, is_proactive=False):
        return False
    async def fake_emit(*a, **k):
        pass
    # 此 task 还没 router，先用 monkeypatch 把 router 跳过
    class _FakeRouter:
        async def route(self, **kw):
            return []
    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _FakeRouter())

    t = ChatTrigger(message_id="m1", session_id="s1")
    await chat_node_mod.route_chat_node(t)  # 不抛异常即可
```

- [ ] **Step 5.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/nodes/test_route_chat_node.py -v
```

Expected: 2 new tests fail。

- [ ] **Step 5.3: 改 route_chat_node**

Modify `apps/agent-service/app/nodes/chat_node.py`:

```python
"""Phase 5a chat 主 pipeline 节点。"""
from __future__ import annotations

import logging
from uuid import uuid4

from app.chat.router import MessageRouter
from app.data.queries import is_chat_request_completed
from app.data.session import get_session
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.runtime.emit import emit
from app.runtime.node import node

logger = logging.getLogger(__name__)


@node
async def route_chat_node(t: ChatTrigger) -> None:
    """ChatTrigger -> ChatRequest fan-out。

    步骤：
      0. 入口校验 message_id 非空
      1. redelivered 短路（is_chat_request_completed helper）
      2. MessageRouter.route 决定 persona 列表（Task 6）
      3. fan-out emit ChatRequest（Task 6）
    """
    if t.message_id is None:
        raise ValueError(
            "ChatTrigger.message_id is None; cannot fan out ChatRequest"
        )

    async with get_session() as s:
        already_done = await is_chat_request_completed(
            s, t.session_id, is_proactive=t.is_proactive
        )
    if already_done:
        logger.info(
            "skip redelivered chat_request: session_id=%s, message_id=%s",
            t.session_id, t.message_id,
        )
        return

    # router + fan-out: Task 6
```

- [ ] **Step 5.4: 跑测试**

```bash
uv run pytest apps/agent-service/tests/nodes/test_route_chat_node.py -v
```

Expected: 3 passed（含 Task 4 的 1 个）。

- [ ] **Step 5.5: commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py apps/agent-service/tests/nodes/test_route_chat_node.py
git commit -m "feat(chat-dataflow): route_chat_node redelivered short-circuit

Calls is_chat_request_completed(s, session_id, is_proactive=...)
helper directly so proactive vs non-proactive routing stays consistent
with chat_consumer.py:79-95 current behavior."
```

---

## Task 6: route_chat_node — MessageRouter + fan-out（多 persona uuid）

**Files:**
- Modify: `apps/agent-service/app/nodes/chat_node.py`
- Modify: `apps/agent-service/tests/nodes/test_route_chat_node.py`

- [ ] **Step 6.1: 写失败测试**

Append to `apps/agent-service/tests/nodes/test_route_chat_node.py`:

```python
@pytest.mark.asyncio
async def test_route_chat_node_single_persona_passes_session_id(monkeypatch):
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(*a, **k): return False

    class _Router:
        async def route(self, **kw): return ["p1"]

    emitted: list[ChatRequest] = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _Router())
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)

    t = ChatTrigger(
        message_id="m1", session_id="s1", chat_id="c1",
        bot_name="bot-x", lane="dev", is_p2p=True,
    )
    await chat_node_mod.route_chat_node(t)

    assert len(emitted) == 1
    r = emitted[0]
    assert r.message_id == "m1"
    assert r.persona_id == "p1"
    assert r.session_id == "s1"  # 第 1 个 persona 透传
    assert r.chat_id == "c1"
    assert r.lane == "dev"
    assert r.bot_name == "bot-x"
    assert r.is_p2p is True


@pytest.mark.asyncio
async def test_route_chat_node_multi_persona_regenerates_session_id(monkeypatch):
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(*a, **k): return False

    class _Router:
        async def route(self, **kw): return ["p1", "p2", "p3"]

    emitted: list[ChatRequest] = []

    async def fake_emit(data): emitted.append(data)

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _Router())
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)

    t = ChatTrigger(message_id="m1", session_id="s1")
    await chat_node_mod.route_chat_node(t)

    assert len(emitted) == 3
    assert emitted[0].session_id == "s1"
    # 第 2/3 个 persona 重生成 uuid，且互不相等
    assert emitted[1].session_id != "s1"
    assert emitted[2].session_id != "s1"
    assert emitted[1].session_id != emitted[2].session_id


@pytest.mark.asyncio
async def test_route_chat_node_empty_persona_list_no_emit(monkeypatch):
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(*a, **k): return False

    class _Router:
        async def route(self, **kw): return []

    emitted = []
    async def fake_emit(d): emitted.append(d)

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _Router())
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)

    t = ChatTrigger(message_id="m1", session_id="s1")
    await chat_node_mod.route_chat_node(t)
    assert emitted == []
```

- [ ] **Step 6.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/nodes/test_route_chat_node.py -v
```

Expected: 3 new tests fail（router/emit 还没接通）。

- [ ] **Step 6.3: 改 route_chat_node**

Replace `route_chat_node` body：

```python
@node
async def route_chat_node(t: ChatTrigger) -> None:
    """ChatTrigger -> ChatRequest fan-out（per persona）。"""
    if t.message_id is None:
        raise ValueError(
            "ChatTrigger.message_id is None; cannot fan out ChatRequest"
        )

    async with get_session() as s:
        already_done = await is_chat_request_completed(
            s, t.session_id, is_proactive=t.is_proactive
        )
    if already_done:
        logger.info(
            "skip redelivered chat_request: session_id=%s, message_id=%s",
            t.session_id, t.message_id,
        )
        return

    router = MessageRouter()
    persona_ids = await router.route(
        chat_id=t.chat_id or "",
        mentions=list(t.mentions),
        bot_name=t.bot_name or "",
        is_p2p=t.is_p2p,
        is_proactive=t.is_proactive,
    )
    if not persona_ids:
        logger.info("no persona to reply: message_id=%s", t.message_id)
        return

    for i, pid in enumerate(persona_ids):
        session_for_persona = t.session_id if i == 0 else str(uuid4())
        await emit(ChatRequest(
            message_id=t.message_id,
            persona_id=pid,
            session_id=session_for_persona,
            chat_id=t.chat_id,
            is_p2p=t.is_p2p,
            root_id=t.root_id,
            user_id=t.user_id,
            is_proactive=t.is_proactive,
            bot_name=t.bot_name,
            lane=t.lane,
            enqueued_at=t.enqueued_at,
        ))
```

- [ ] **Step 6.4: 跑测试**

```bash
uv run pytest apps/agent-service/tests/nodes/test_route_chat_node.py -v
```

Expected: 6 passed。

- [ ] **Step 6.5: commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py apps/agent-service/tests/nodes/test_route_chat_node.py
git commit -m "feat(chat-dataflow): route_chat_node fan-out per persona

Single persona reuses trigger.session_id; second and beyond regenerate
uuid4. Mirrors chat_consumer.py:131 current behavior. Empty persona
list returns silently."
```

---

## Task 7: chat_node — prep 块（fetch + parse + gray + guard + pre_task）

**Files:**
- Modify: `apps/agent-service/app/nodes/chat_node.py`
- Create: `apps/agent-service/tests/nodes/test_chat_node.py`

- [ ] **Step 7.1: 写失败测试**

Create `apps/agent-service/tests/nodes/test_chat_node.py`:

```python
"""chat_node 单元测试（Task 7-11 累积）。"""
import pytest

from app.domain.chat_dataflow import ChatRequest


@pytest.fixture
def base_request():
    return ChatRequest(
        message_id="m1", persona_id="p1", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="dev",
    )


@pytest.mark.asyncio
async def test_chat_node_prep_block_calls_dependencies(monkeypatch, base_request):
    """prep 块按顺序调用 find_message_content / parse_content / find_gray_config /
    fetch_guard_message / run_pre_safety_via_graph。"""
    from app.nodes import chat_node as cn

    calls = []

    async def fake_find_message(s, mid):
        calls.append(("find_message_content", mid))
        return "hello world"
    async def fake_find_gray(s, mid):
        calls.append(("find_gray_config", mid))
        return {"gray": "x"}
    async def fake_guard(persona):
        calls.append(("fetch_guard_message", persona))
        return "guard say no"
    async def fake_pre_safety(message_id, content, persona_id):
        calls.append(("run_pre_safety_via_graph", message_id, persona_id))
        from app.chat.pre_safety_gate import PreSafetyVerdict
        return PreSafetyVerdict(
            pre_request_id="x", message_id=message_id, is_blocked=False,
        )
    async def fake_build_and_stream(*a, **k):
        if False:
            yield ""
    async def fake_resolve_bot(s, pid, cid): return "resolved-bot"
    async def fake_set_bot(s, sid, bn, pid): pass
    async def fake_emit(d): pass

    def parse_content_fn(s):
        calls.append(("parse_content", s))
        class _P:
            def render(self): return s
        return _P()

    monkeypatch.setattr(cn, "find_message_content", fake_find_message)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre_safety)
    monkeypatch.setattr(cn, "parse_content", parse_content_fn)
    monkeypatch.setattr(cn, "_build_and_stream", fake_build_and_stream)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve_bot)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set_bot)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    names = [c[0] for c in calls]
    assert "find_message_content" in names
    assert "parse_content" in names
    assert "find_gray_config" in names
    assert "fetch_guard_message" in names
    assert "run_pre_safety_via_graph" in names
    assert names.index("find_message_content") < names.index("parse_content")
    assert names.index("parse_content") < names.index("run_pre_safety_via_graph")
```

- [ ] **Step 7.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py -v
```

Expected: 1 failed（chat_node 还是 NotImplementedError）。

- [ ] **Step 7.3: 改 chat_node 加 prep 块**

Modify `apps/agent-service/app/nodes/chat_node.py` —— 顶部加 import：

```python
import asyncio
import time
from typing import AsyncGenerator

from app.chat.content_parser import parse_content
from app.chat.post_actions import fetch_guard_message
from app.chat.pre_safety_gate import run_pre_safety_via_graph
from app.data.queries import (
    find_gray_config,
    find_message_content,
    is_chat_request_completed,
    resolve_bot_name_for_persona,
    set_agent_response_bot,
)
```

替换 `chat_node`:

```python
@node
async def chat_node(req: ChatRequest) -> None:
    """ChatRequest -> N × ChatResponseSegment (per persona).

    Phases (内部分块，不拆 node):
      1. prep: fetch + parse + gray + guard + pre_task 启动
      2. fetch 为空 -> emit 1 段 "未找到" + return  (Task 8)
      3. resolve response_bot_name + agent_responses 行更新  (Task 9)
      4. base_payload 构造（含 lane）  (Task 9)
      5. 主循环 + 中段 emit  (Task 10)
      6. final 段 + pre-safety blocked 路径  (Task 11)
    """
    # 1. prep
    async with get_session() as s:
        raw_content = await find_message_content(s, req.message_id)
    parsed = parse_content(raw_content) if raw_content else None
    async with get_session() as s:
        gray_config = (await find_gray_config(s, req.message_id)) or {}
    effective_persona = req.persona_id or req.bot_name or ""
    guard_message = await fetch_guard_message(effective_persona)
    pre_task = asyncio.create_task(
        run_pre_safety_via_graph(
            message_id=req.message_id,
            content=parsed.render() if parsed else "",
            persona_id=effective_persona,
        )
    )

    # 后续 task 加 fetch-empty 早返回 / bot resolve / 主循环 / final
```

- [ ] **Step 7.4: 跑测试**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py::test_chat_node_prep_block_calls_dependencies -v
```

Expected: 1 passed.

- [ ] **Step 7.5: commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py apps/agent-service/tests/nodes/test_chat_node.py
git commit -m "feat(chat-dataflow): chat_node prep block

fetch message / parse / gray config / guard message / pre_safety task —
ported from chat/pipeline.py:78-110 stream_chat header. Subsequent
tasks add fetch-empty short-circuit, bot resolve, main loop, final
segment."
```

---

## Task 8: chat_node — 找不到消息早返回

**Files:**
- Modify: `apps/agent-service/app/nodes/chat_node.py`
- Modify: `apps/agent-service/tests/nodes/test_chat_node.py`

- [ ] **Step 8.1: 写失败测试**

Append to `apps/agent-service/tests/nodes/test_chat_node.py`:

```python
@pytest.mark.asyncio
async def test_chat_node_emits_not_found_when_no_message(monkeypatch, base_request):
    from app.nodes import chat_node as cn
    from app.domain.chat_dataflow import ChatResponseSegment

    async def fake_find_message(s, mid): return None
    async def fake_find_gray(s, mid): return {}
    async def fake_guard(persona): return "guard"
    async def fake_pre(*a, **k):
        from app.chat.pre_safety_gate import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)

    monkeypatch.setattr(cn, "find_message_content", fake_find_message)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)

    emitted: list[ChatResponseSegment] = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert len(emitted) == 1
    seg = emitted[0]
    assert "未找到相关消息记录" in seg.content
    assert seg.is_last is True
    assert seg.message_id == "m1"
    assert seg.persona_id == "p1"
    assert seg.lane == "dev"
```

- [ ] **Step 8.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py::test_chat_node_emits_not_found_when_no_message -v
```

Expected: 1 failed（chat_node 还没实现 fetch-empty 路径）。

- [ ] **Step 8.3: 改 chat_node**

替换 `chat_node` 函数体（保留 prep 那段，在 prep 后加 fetch-empty 早返回）：

```python
    # 2. fetch 为空 -> emit 1 段 "未找到" + return
    if not raw_content:
        await emit(ChatResponseSegment(
            message_id=req.message_id,
            persona_id=req.persona_id,
            part_index=0,
            session_id=req.session_id,
            chat_id=req.chat_id,
            is_p2p=req.is_p2p,
            root_id=req.root_id,
            user_id=req.user_id,
            is_proactive=req.is_proactive,
            bot_name=req.bot_name,
            lane=req.lane,
            content="抱歉，未找到相关消息记录",
            status="success",
            is_last=True,
            full_content=None,
            published_at=int(time.time() * 1000),
        ))
        pre_task.cancel()
        return
```

- [ ] **Step 8.4: 跑测试**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py -v
```

Expected: 2 passed。

- [ ] **Step 8.5: commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py apps/agent-service/tests/nodes/test_chat_node.py
git commit -m "feat(chat-dataflow): chat_node fetch-empty short-circuit

Mirror pipeline.py:80-83 behavior — emit one is_last segment
'未找到相关消息记录' and cancel the pre-safety task."
```

---

## Task 9: chat_node — resolve bot_name + base_payload + agent_responses

**Files:**
- Modify: `apps/agent-service/app/nodes/chat_node.py`
- Modify: `apps/agent-service/tests/nodes/test_chat_node.py`

- [ ] **Step 9.1: 写失败测试**

Append to `apps/agent-service/tests/nodes/test_chat_node.py`:

```python
@pytest.mark.asyncio
async def test_chat_node_resolves_bot_name_and_updates_agent_response(monkeypatch, base_request):
    from app.nodes import chat_node as cn

    async def fake_find_message(s, mid): return "hi"
    async def fake_find_gray(s, mid): return {}
    async def fake_guard(persona): return "guard"
    async def fake_pre(*a, **k):
        from app.chat.pre_safety_gate import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)
    async def fake_build_and_stream(*a, **k):
        if False:
            yield ""

    resolved_calls = []
    set_calls = []
    async def fake_resolve(s, persona_id, chat_id):
        resolved_calls.append((persona_id, chat_id))
        return "resolved-bot-x"
    async def fake_set(s, session_id, bot_name, persona_id):
        set_calls.append((session_id, bot_name, persona_id))

    monkeypatch.setattr(cn, "find_message_content", fake_find_message)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_build_and_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert resolved_calls == [("p1", "c1")]
    assert set_calls == [("s1", "resolved-bot-x", "p1")]
```

- [ ] **Step 9.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py::test_chat_node_resolves_bot_name_and_updates_agent_response -v
```

Expected: failed.

- [ ] **Step 9.3: 改 chat_node 加 resolve_bot + set_agent_response_bot + base_payload**

在 `chat_node` 函数 fetch-empty 之后添加：

```python
    # 3. resolve response_bot_name + 更新 agent_responses 行
    async with get_session() as s:
        response_bot_name = await resolve_bot_name_for_persona(
            s, req.persona_id, req.chat_id or "",
        )
    if not response_bot_name:
        response_bot_name = req.bot_name or ""
    if req.session_id:
        try:
            async with get_session() as s:
                await set_agent_response_bot(
                    s, req.session_id, response_bot_name, req.persona_id,
                )
        except Exception as e:
            logger.warning("Failed to update agent_response: %s", e)

    # 4. base_payload (segments 共用字段)
    base_payload = dict(
        message_id=req.message_id,
        persona_id=req.persona_id,
        session_id=req.session_id,
        chat_id=req.chat_id,
        is_p2p=req.is_p2p,
        root_id=req.root_id,
        user_id=req.user_id,
        is_proactive=req.is_proactive,
        bot_name=response_bot_name,
        lane=req.lane,  # CRITICAL: sink 不会自动注入 header lane
    )

    # 主循环 + final: Task 10/11
```

- [ ] **Step 9.4: 跑测试**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py -v
```

Expected: 3 passed。

- [ ] **Step 9.5: commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py apps/agent-service/tests/nodes/test_chat_node.py
git commit -m "feat(chat-dataflow): chat_node resolves bot_name and updates agent_response

Ported from chat_consumer.py:155-186. base_payload includes lane
explicitly because Sink.mq does not inject header lane into body."
```

---

## Task 10: chat_node — 主流 + 中段 emit + 搬 _build_and_stream

**Files:**
- Modify: `apps/agent-service/app/nodes/chat_node.py`
- Modify: `apps/agent-service/tests/nodes/test_chat_node.py`

> **Note**: 这一步把 `_build_and_stream` 整体从 `apps/agent-service/app/chat/pipeline.py` 搬到 `apps/agent-service/app/nodes/chat_node.py`。pipeline.py 不动（它的 stream_chat 还在用着），等 Task 12 整体删除。这里 chat_node 通过 `from app.chat.pipeline import _build_and_stream` 临时桥接 —— 不需要真的搬。

- [ ] **Step 10.1: 写失败测试**

Append to `apps/agent-service/tests/nodes/test_chat_node.py`:

```python
SPLIT = "---split---"


@pytest.mark.asyncio
async def test_chat_node_split_two_segments_then_final(monkeypatch, base_request):
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.chat.pre_safety_gate import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)

    async def fake_stream(*a, **k):
        for piece in ["hello ", SPLIT, " world", SPLIT, " foo"]:
            yield piece

    async def fake_resolve(s, p, c): return "bot-x"
    async def fake_set(s, sid, bn, pid): pass
    async def fake_find_msg(s, mid): return "input"
    async def fake_find_gray(s, mid): return {}
    async def fake_guard(p): return "guard"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    # 三段：part_index 0, 1, 2; 最后一段 is_last=True
    assert len(emitted) == 3
    assert emitted[0].part_index == 0
    assert emitted[0].content == "hello"
    assert emitted[0].is_last is False
    assert emitted[1].part_index == 1
    assert emitted[1].content == "world"
    assert emitted[1].is_last is False
    assert emitted[2].part_index == 2
    assert emitted[2].is_last is True
    assert "foo" in emitted[2].content
    # final 段必须带 full_content（清理过 SPLIT_MARKER）
    assert emitted[2].full_content is not None
    assert SPLIT not in emitted[2].full_content
    # 全部带 lane
    for s in emitted:
        assert s.lane == "dev"
        assert s.bot_name == "bot-x"
```

- [ ] **Step 10.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py::test_chat_node_split_two_segments_then_final -v
```

Expected: failed（无主循环）。

- [ ] **Step 10.3: 改 chat_node 加主循环 + final 段**

在 `chat_node` base_payload 之后加：

```python
    # 5. 主循环 + 中段 emit
    SPLIT_MARKER = "---split---"
    MAX_MESSAGES = 4

    sent_length = 0
    part_index = 0
    full_content = ""

    async for text in _build_and_stream(
        req.message_id,
        gray_config,
        session_id=req.session_id,
        persona_id=req.persona_id,
    ):
        if not text:
            continue
        full_content += text
        pending = full_content[sent_length:]
        while SPLIT_MARKER in pending and part_index < MAX_MESSAGES - 1:
            idx = pending.index(SPLIT_MARKER)
            part = pending[:idx].strip()
            if part:
                # Task 11 加 pre-safety blocked 路径，本 task 暂直放
                await emit(ChatResponseSegment(
                    **base_payload,
                    part_index=part_index,
                    content=part,
                    status="success",
                    is_last=False,
                    full_content=None,
                    published_at=int(time.time() * 1000),
                ))
                part_index += 1
            sent_length += idx + len(SPLIT_MARKER)
            pending = full_content[sent_length:]

    # 6. final 段 (Task 11 加 blocked 路径)
    remaining = full_content[sent_length:].replace(SPLIT_MARKER, "").strip()
    clean_full = full_content.replace(SPLIT_MARKER, "\n\n").strip()
    final_content = (
        (remaining or full_content) if (remaining or part_index == 0) else ""
    )
    await emit(ChatResponseSegment(
        **base_payload,
        part_index=part_index,
        content=final_content,
        status="success",
        is_last=True,
        full_content=clean_full,
        published_at=int(time.time() * 1000),
    ))

    # pre_task 还可能 pending，让它自然完成
    if not pre_task.done():
        try:
            await asyncio.wait_for(pre_task, timeout=0.1)
        except (asyncio.TimeoutError, Exception):
            pre_task.cancel()
```

加 import 在文件顶部：

```python
from app.chat.pipeline import _build_and_stream  # 临时桥接，Task 12 搬入本文件
```

- [ ] **Step 10.4: 跑测试**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py -v
```

Expected: 4 passed.

- [ ] **Step 10.5: commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py apps/agent-service/tests/nodes/test_chat_node.py
git commit -m "feat(chat-dataflow): chat_node main stream loop + segment emit

Mirrors chat_consumer.py:204-281 — accumulate _build_and_stream str
yields, split on SPLIT_MARKER, emit ChatResponseSegment per part. lane
is carried from req into every segment. _build_and_stream is imported
from app.chat.pipeline for now; will be moved into this module in
Task 12."
```

---

## Task 11: chat_node — pre-safety blocked 路径（_resolve_pre_safety_for_part helper）

**Files:**
- Modify: `apps/agent-service/app/nodes/chat_node.py`
- Modify: `apps/agent-service/tests/nodes/test_chat_node.py`

- [ ] **Step 11.1: 写失败测试**

Append to `apps/agent-service/tests/nodes/test_chat_node.py`:

```python
@pytest.mark.asyncio
async def test_chat_node_pre_safety_block_at_first_boundary(monkeypatch, base_request):
    """verdict=BLOCK 在第一个段边界返回 -> emit 1 段 guard + is_last=True，无后续。"""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.chat.pre_safety_gate import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=True)

    async def fake_stream(*a, **k):
        # 一段 + SPLIT；正常情况是 emit "hello"，blocked 时改 emit guard
        for p in ["hello", SPLIT, " world", SPLIT, " final"]:
            yield p

    async def fake_resolve(s, p, c): return "bot-x"
    async def fake_set(*a, **k): pass
    async def fake_find_msg(s, mid): return "input"
    async def fake_find_gray(s, mid): return {}
    async def fake_guard(p): return "GUARD_TEXT"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    # 飞书侧只看到一段 guard + is_last=True
    assert len(emitted) == 1
    assert emitted[0].content == "GUARD_TEXT"
    assert emitted[0].is_last is True
    assert emitted[0].full_content == "GUARD_TEXT"


@pytest.mark.asyncio
async def test_chat_node_pre_safety_block_at_final(monkeypatch, base_request):
    """stream 已结束（无 SPLIT），verdict 在 final 段到达时为 BLOCK。"""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.chat.pre_safety_gate import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=True)

    async def fake_stream(*a, **k):
        for p in ["just one piece"]:
            yield p

    async def fake_resolve(s, p, c): return "bot-x"
    async def fake_set(*a, **k): pass
    async def fake_find_msg(s, mid): return "input"
    async def fake_find_gray(s, mid): return {}
    async def fake_guard(p): return "GUARD_TEXT"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert len(emitted) == 1
    assert emitted[0].content == "GUARD_TEXT"
    assert emitted[0].is_last is True
```

- [ ] **Step 11.2: 跑测试，确认失败**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py -v -k "pre_safety_block"
```

Expected: 2 failed（chat_node 还没接 pre-safety blocked 处理）。

- [ ] **Step 11.3: 加 _resolve_pre_safety_for_part helper**

在 chat_node 之前加：

```python
from dataclasses import dataclass


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

    fail-open（pre_task 抛 / timeout）-> ALLOW（与 _buffer_until_pre 现行
    fail-open 行为一致）。
    """
    if not pre_task.done():
        try:
            await asyncio.wait_for(pre_task, timeout=timeout)
        except asyncio.TimeoutError:
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

- [ ] **Step 11.4: 改主循环 + final 段，引入 blocked 早终止**

Replace 主循环 + final 段（Task 10 加进去的）：

```python
    # 5. 主循环 + 中段 emit (with pre-safety BLOCK termination)
    sent_length = 0
    part_index = 0
    full_content = ""

    async def _emit_block_guard():
        await emit(ChatResponseSegment(
            **base_payload,
            part_index=part_index,
            content=guard_message,
            status="success",
            is_last=True,
            full_content=guard_message,
            published_at=int(time.time() * 1000),
        ))

    async for text in _build_and_stream(
        req.message_id,
        gray_config,
        session_id=req.session_id,
        persona_id=req.persona_id,
    ):
        if not text:
            continue
        full_content += text
        pending = full_content[sent_length:]
        while SPLIT_MARKER in pending and part_index < MAX_MESSAGES - 1:
            idx = pending.index(SPLIT_MARKER)
            part = pending[:idx].strip()
            if part:
                result = await _resolve_pre_safety_for_part(
                    part, pre_task, guard_message,
                )
                if result.blocked:
                    await _emit_block_guard()
                    return
                await emit(ChatResponseSegment(
                    **base_payload,
                    part_index=part_index,
                    content=result.content,
                    status="success",
                    is_last=False,
                    full_content=None,
                    published_at=int(time.time() * 1000),
                ))
                part_index += 1
            sent_length += idx + len(SPLIT_MARKER)
            pending = full_content[sent_length:]

    # 6. final 段
    remaining = full_content[sent_length:].replace(SPLIT_MARKER, "").strip()
    clean_full = full_content.replace(SPLIT_MARKER, "\n\n").strip()
    final_content = (
        (remaining or full_content) if (remaining or part_index == 0) else ""
    )
    result = await _resolve_pre_safety_for_part(
        final_content, pre_task, guard_message,
    )
    if result.blocked:
        await _emit_block_guard()
        return
    await emit(ChatResponseSegment(
        **base_payload,
        part_index=part_index,
        content=result.content,
        status="success",
        is_last=True,
        full_content=clean_full,
        published_at=int(time.time() * 1000),
    ))
```

把之前 step 10 的 "pre_task 还可能 pending..." 那段 cleanup 删掉（已被 helper 内的 await 取代）。

- [ ] **Step 11.5: 跑测试**

```bash
uv run pytest apps/agent-service/tests/nodes/test_chat_node.py -v
```

Expected: 6 passed（含之前 4 个）。

- [ ] **Step 11.6: commit**

```bash
git add apps/agent-service/app/nodes/chat_node.py apps/agent-service/tests/nodes/test_chat_node.py
git commit -m "feat(chat-dataflow): chat_node pre-safety BLOCK termination

_resolve_pre_safety_for_part helper waits at segment boundary; on
BLOCK chat_node emits one guard segment with is_last=True and returns.
fail-open on timeout / exception. Aligns with §4.2 invariant 3."
```

---

## Task 12: 删 chat_consumer.py + main.py + chat/__init__.py + chat/pipeline.py

**Files:**
- Delete: `apps/agent-service/app/workers/chat_consumer.py`
- Modify: `apps/agent-service/app/main.py`
- Modify: `apps/agent-service/app/chat/__init__.py`
- Delete: `apps/agent-service/app/chat/pipeline.py`（如果整体不再被引用）
- Modify: `apps/agent-service/app/nodes/chat_node.py`（搬入 `_build_and_stream` 实现）

- [ ] **Step 12.1: 把 _build_and_stream 整体搬入 chat_node.py**

打开 `apps/agent-service/app/chat/pipeline.py`，把 `_build_and_stream`（line 128 起）整段函数 + 它依赖的所有 helper（`_load_bot_context` 之类，看 Imports）整段复制到 `apps/agent-service/app/nodes/chat_node.py` 末尾。

修改 chat_node.py 顶部：把 `from app.chat.pipeline import _build_and_stream` 删掉。

> **执行提示**：搬运过程逐函数 grep。下列符号在 pipeline.py 里被定义、且被 _build_and_stream 引用的，都要搬：
> - `_build_and_stream` 自身
> - 任何只被 _build_and_stream 用的 helper（`_load_bot_context`、`_STREAM_END` 等）
> - 顶部 import：`AgentConfig`, `Agent`, `AIMessageChunk`, `ToolMessage`, `propagate_attributes`, `get_langfuse`, `header_vars`, `CHAT_PIPELINE_DURATION`, `CHAT_TOKENS`, `build_inner_context` 等
>
> 搬完后跑：
> ```bash
> grep -n "from app.chat.pipeline" apps/agent-service/app/ -r --include="*.py"
> ```
> 应该只剩 `chat/__init__.py` 一处（下一步删除）。

- [ ] **Step 12.2: 跑全套测试确认搬迁不破坏行为**

```bash
uv run pytest apps/agent-service/tests/ -x --timeout=30
```

Expected: 全部 pass。

- [ ] **Step 12.3: commit（搬迁中间态）**

```bash
git add apps/agent-service/app/nodes/chat_node.py
git commit -m "refactor(chat-dataflow): move _build_and_stream into chat_node module

Preparation for deleting chat/pipeline.py. Tests still green."
```

- [ ] **Step 12.4: 删 chat_consumer.py**

```bash
git rm apps/agent-service/app/workers/chat_consumer.py
```

- [ ] **Step 12.5: 改 main.py 删 start_chat_consumer 调用**

定位 main.py 里 `start_chat_consumer` 的 import + 调用（约 line 76-82），删掉这两行。如果 main.py 顶部还有 `from app.workers.chat_consumer import start_chat_consumer`，也删。

```bash
grep -n "chat_consumer\|start_chat_consumer" apps/agent-service/app/main.py
```

按 grep 输出位置删除对应行。

- [ ] **Step 12.6: 改 chat/__init__.py**

打开 `apps/agent-service/app/chat/__init__.py`，删除 `from app.chat.pipeline import stream_chat` 那一行；如果 `__all__` 里有 `"stream_chat"` 也删。MessageRouter 的 export 保留（route_chat_node 在用）。

- [ ] **Step 12.7: 删 chat/pipeline.py**

```bash
grep -rn "from app.chat.pipeline\|import app.chat.pipeline" apps/agent-service/ --include="*.py"
```

如果 grep 输出只剩 chat_node.py（已经在 step 12.1 删了 import），可以直接：

```bash
git rm apps/agent-service/app/chat/pipeline.py
```

如果 grep 还有别处引用，逐个分析：
- 如果是 stream_chat：肯定不能用，删调用方
- 如果是 _build_and_stream 之类的私有 helper：搬走或删

- [ ] **Step 12.8: 跑全套测试**

```bash
uv run pytest apps/agent-service/tests/ -x --timeout=30
```

Expected: 全部 pass。

- [ ] **Step 12.9: commit**

```bash
git add -A
git commit -m "refactor(chat-dataflow): delete chat_consumer.py + chat/pipeline.py + cleanup

main.py no longer starts chat_consumer; chat/__init__.py drops
stream_chat export. The dataflow source loop on chat_request queue
takes over. End-to-end behavior verified by unit tests."
```

---

## Task 13: dedup 测试 + capture_emit fixture + grep 自检

**Files:**
- Modify: `apps/agent-service/tests/conftest.py`（如不存在则 Create）
- Create: `apps/agent-service/tests/dataflow/__init__.py`
- Create: `apps/agent-service/tests/dataflow/test_chat_dedup.py`

- [ ] **Step 13.1: 加 capture_emit fixture（如果 conftest 没有）**

```bash
grep -n "capture_emit" apps/agent-service/tests/conftest.py 2>/dev/null
```

如果 grep 无输出，在 `apps/agent-service/tests/conftest.py` 末尾追加：

```python
@pytest.fixture
def capture_emit(monkeypatch):
    """Capture every emit() call into a list. Returns the list."""
    seen = []
    async def _fake_emit(data):
        seen.append(data)
    # 同时 patch 所有 caller 模块里的 emit
    import app.runtime.emit as emit_mod
    monkeypatch.setattr(emit_mod, "emit", _fake_emit)
    return seen
```

- [ ] **Step 13.2: 创建 tests/dataflow 目录**

```bash
mkdir -p apps/agent-service/tests/dataflow
touch apps/agent-service/tests/dataflow/__init__.py
```

- [ ] **Step 13.3: 写 dedup 测试**

Create `apps/agent-service/tests/dataflow/test_chat_dedup.py`:

```python
"""Phase 5a: dedup 层级测试。

ChatTrigger 自身不参与 dedup（source.mq 入口无 insert_idempotent）；
真实 dedup 在 ChatRequest 这一层（in-graph durable wire）。
"""
import pytest

from app.domain.chat_dataflow import ChatRequest, ChatTrigger


@pytest.mark.asyncio
async def test_chat_request_idempotent_blocks_second_emit(tmp_path):
    """同一 (message_id, persona_id) 的 ChatRequest insert 第二次返 0。"""
    pytest.importorskip("asyncpg")  # runtime durable layer 用 pg
    # 这一段需要 in-memory pg 或者真实 pg session；如果没有，使用 runtime 提供的
    # 测试 helper（Phase 4 也有类似断言，看 tests/dataflow/test_*.py 模板）。
    from app.runtime.persist import insert_idempotent
    r1 = ChatRequest(message_id="m1", persona_id="p1")
    r2 = ChatRequest(message_id="m1", persona_id="p1")
    n1 = await insert_idempotent(r1)
    n2 = await insert_idempotent(r2)
    assert n1 == 1
    assert n2 == 0


@pytest.mark.asyncio
async def test_chat_trigger_does_not_have_table():
    """ChatTrigger transient=True -> migrator 不建表，runtime 不会 insert_idempotent。"""
    from app.runtime.migrator import collect_data_classes
    classes = collect_data_classes()
    assert ChatTrigger not in classes
```

> **note**: 第 1 个测试需要 pg 真实连接。如果项目里 `tests/dataflow/` 模板用的是 mock 而不是真 pg，参考 Phase 3/4 的 `test_*_durable.py` 测试用什么 fixture，按那个模板改。如果用 mock，断言改成"对 insert_idempotent 的 patched 调用第二次因检测到 (m1, p1) 已存在返 0"。

- [ ] **Step 13.4: 跑专项测试**

```bash
uv run pytest apps/agent-service/tests/dataflow/test_chat_dedup.py -v
```

Expected: 2 passed。

- [ ] **Step 13.5: 跑 ship 前 grep 自检**

```bash
echo "=== should be 0 ===" && \
grep -rn "stream_chat\|workers/chat_consumer" apps/agent-service/ --include="*.py" || true && \
echo "---" && \
grep -rn "from app.runtime.stream\|app.runtime.stream\|Stream\\[" apps/agent-service/ --include="*.py" || true && \
echo "---" && \
grep -rn "AsyncGenerator\[str" apps/agent-service/app/chat/ --include="*.py" || true && \
ls apps/agent-service/app/runtime/stream.py 2>&1 || echo "stream.py: not present (good)"
```

Expected:
- 前三个 grep 无输出（除非命中 spec / plan 文档自身的字符串引用，那不算违规，但应当用 `--include="*.py"` 排除）
- `ls runtime/stream.py` 提示 not present

如果有残留，逐处处理。

- [ ] **Step 13.6: 跑全套测试 + 断言 wiring 编译**

```bash
uv run pytest apps/agent-service/tests/ -x --timeout=60
```

Expected: 全部 pass。

- [ ] **Step 13.7: commit**

```bash
git add apps/agent-service/tests/dataflow/ apps/agent-service/tests/conftest.py
git commit -m "test(chat-dataflow): dedup layer assertions + grep guard

ChatRequest is the real dedup layer (in-graph durable wire); ChatTrigger
is transient and not deduped at source.mq input. capture_emit fixture
unifies emit interception."
```

---

## Task 14: 泳道部署 + e2e（用户执行）

> **重要**：本任务步骤需要用户登录终端 / 飞书 dev bot 操作，不是 AI 单方面可完成。AI 把命令打出来给用户执行，并等用户给出结果。AI 不要自动 deploy / undeploy，按项目 CLAUDE.md "禁止未经许可上线"准则执行。

- [ ] **Step 14.1: 把当前分支 push 到远端**

```bash
git push origin refactor/flow-parse-5
```

- [ ] **Step 14.2: 部署到 dev 泳道**

```bash
make deploy APP=agent-service GIT_REF=refactor/flow-parse-5 LANE=feat-flow-parse-5
```

构建完成后等 PaaS 部署 prod ready。完成后**同步 release** arq-worker / vectorize-worker（项目铁律 4，agent-service 镜像产 3 deployment）：

```bash
make latest-build APP=agent-service  # 拿到刚构建的 VERSION
# 假设 VERSION = 1.0.0.323，下面命令用实际值替换
make release APP=arq-worker LANE=feat-flow-parse-5 VERSION=<VERSION>
make release APP=vectorize-worker LANE=feat-flow-parse-5 VERSION=<VERSION>
```

- [ ] **Step 14.3: 绑定 dev bot**

```
/ops bind TYPE=bot KEY=dev LANE=feat-flow-parse-5
```

- [ ] **Step 14.4: e2e 测试 4 个场景**

在飞书 dev bot 实测：

1. **单聊普通对话**：随便发一条话，验证赤尾分多段回复
2. **单聊问需要工具的**：发"帮我搜下 xxx"，验证 tool 穿插的 split 段
3. **群聊 @ 赤尾对话**：群里 @ 赤尾，验证群聊路径
4. **故意触发 pre-safety**：发已知会被 pre-safety 拦的话术，验证只收到一段 guard message

每个场景跑通，记录飞书界面截图或文字反馈。

- [ ] **Step 14.5: 监控 prod 三表 1h**

```sql
-- 通过 /ops-db @chiwei 查
SELECT count(*), persona_id FROM data_chat_request WHERE created_at > now() - interval '30 min' GROUP BY persona_id;
```

应该能看到 message_id × persona_id 的 dedup 表行。

```bash
/ops pods agent-service feat-flow-parse-5
```

应该 Running、0 restart。

- [ ] **Step 14.6: 等用户验收 + 解绑 + 下泳道**

等用户明确说"OK 验收完毕"。然后：

```
/ops unbind TYPE=bot KEY=dev
```

```bash
make undeploy APP=agent-service LANE=feat-flow-parse-5
make undeploy APP=arq-worker LANE=feat-flow-parse-5
make undeploy APP=vectorize-worker LANE=feat-flow-parse-5
```

- [ ] **Step 14.7: 创建 PR**

```bash
ghc pr create --title "feat(dataflow): Phase 5a — chat main pipeline into graph" --body "$(cat <<'EOF'
## Summary
- Replace workers/chat_consumer.py + chat/pipeline.py:stream_chat with route_chat_node + chat_node + chat wiring.
- Drop runtime/stream.py + node.py Stream check (unused since Phase 0 — fan-out in @node body is the canonical pattern in use since Phase 4).
- ChatRequest gains a data_chat_request dedup table; mq redelivery no longer reruns the LLM.
- DLQ replay caveat documented: dedup row already written before handler, direct replay is no-op by default.

## Test plan
- [x] Unit: test_chat_dataflow / test_route_chat_node / test_chat_node / test_chat_wiring / test_chat_dedup
- [x] Migrator: data_chat_request created at boot; ChatTrigger / ChatResponseSegment skipped
- [x] Dev lane e2e: P2P normal / P2P tool-call / group @ / pre-safety block — all four scenarios verified
- [x] Prod 1h observation: data_chat_request rows match message rate, agent-service pod 0 restart

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 14.8: 等用户许可 ship**

按项目 CLAUDE.md "合码必须等用户确认"准则，PR 创建后等用户明确说"合"或"merge"。AI 不主动合并。

---

## 验收清单（执行人最终核对）

5a 完成判定（spec §8 验收清单 5a 部分对应）：

- [ ] `compile_graph()` 通过（含 ChatTrigger / ChatRequest / ChatResponseSegment 三条 wire）
- [ ] runtime migrator 自动建出 `data_chat_request` 表（boot 时 + integration test 双重验证）；ChatTrigger 不建表
- [ ] 单元测试全绿（test_chat_dataflow / test_route_chat_node / test_chat_node / test_chat_wiring / test_chat_dedup）
- [ ] grep 自检全部为 0（Task 13 step 5）
- [ ] 飞书 dev bot 4 个场景通过（单聊 / 单聊工具 / 群聊 / pre-safety 拦截）
- [ ] prod 部署后 1h 观察无异常
- [ ] 现状全部行为不变量（spec §4.2）保持
- [ ] DLQ 监控告警就位（plan 阶段确认；如果没到位先做这个）

---

## 后续（非本 plan 范围）

- 5b: 删 `app/bridges/` + proactive.py 改 `emit(Message.from_cm(cm))`
- Phase 6: agent tool 副作用进 wire（commit_abstract_memory → emit AbstractMemorySaved）
- "chat 部分段后中断、新 Pod 不续传" 改进（需 chat_node 状态持久化）
- 应用层 redelivered（is_chat_request_completed）vs runtime idempotent 双层兜底是否过头评估
