# Dataflow Phase 2 — Safety 管线进 Graph 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 agent-service 的 safety_pre / safety_post 改造成 dataflow graph 节点 + wire；消灭 `mq.publish(SAFETY_CHECK,...)`；让 Recall 通过 `Sink.mq("recall")` 出 graph 给 lark-server。

**Architecture:**
- **Pre-check 控制面进 graph**：4 条 wire 串起 `PreSafetyRequest -> run_pre_safety -> PreSafetyVerdict -> resolve_pre_safety_waiter`，chat pipeline 通过本地 Future waiter 拿回 verdict，race 行为保留
- **Post-check 数据面走 durable**：`PostSafetyRequest --durable--> run_post_safety -> Recall | None` + `Recall -> Sink.mq("recall")`，PostSafetyRequest **adopt `agent_responses` 表**，业务侧用 `safety_status` 字段做幂等
- **Runtime 携带小增量**：放开 sink dispatch + 启动期校验所有 `Sink.mq(name)` 在 `ALL_ROUTES`
- **跨镜像**：agent-service 主体改造 + lark-server `recall-worker.ts` 5 行修复（max retry 分支补 `recall_failed`）

**Tech Stack:** Python 3.12 / Pydantic v2 / SQLAlchemy 2 async / aio-pika (RabbitMQ) / pytest-asyncio / TypeScript / TypeORM

**Reference spec:** `docs/superpowers/specs/2026-04-28-dataflow-phase-2-safety-design.md`

---

## File Structure

**新建（apps/agent-service/app/）：**

```
domain/
    safety.py                     # 4 个 Data 类：PreSafetyRequest / PreSafetyVerdict / PostSafetyRequest / Recall

nodes/
    safety.py                     # 3 个节点 + 私有 helper（合并 chat/safety.py）：
                                  #   - module-level: _check_banned_word / _check_injection /
                                  #     _check_politics / _check_nsfw / _check_output / _run_audit
                                  #   - module-level: BlockReason enum, _GUARD_* AgentConfig
                                  #   - @node: run_pre_safety / resolve_pre_safety_waiter / run_post_safety
                                  #   - 常量: TERMINAL_STATUSES = {passed, blocked, recalled, recall_failed}

chat/
    pre_safety_gate.py            # 本地 waiter registry：register / resolve / cleanup +
                                  # run_pre_safety_via_graph 协调函数

wiring/
    safety.py                     # 4 条 wire 声明 + bind() 把 4 个节点贴到 agent-service

runtime/
    sink_dispatch.py              # _dispatch_mq_sink + _route_by_queue
```

**修改（apps/agent-service/app/）：**

```
runtime/
    graph.py                      # 删除 sinks unimplemented；加 sink queue 在 ALL_ROUTES 校验
    emit.py                       # wire 循环里加 sink dispatch 分支
    placement.py                  # 通过 wiring/safety.py 隐式 bind（不直接改这里，只用 bind() DSL）

data/
    queries.py                    # 新增 get_safety_status()

chat/
    post_actions.py               # _publish_post_check 改成 emit(PostSafetyRequest(...))
    pipeline.py                   # pre_task 改调 run_pre_safety_via_graph；
                                  # _buffer_until_pre 读 verdict 字段名调整

main.py                           # lifespan 启动 start_consumers(app_name="agent-service")
                                  # 删除 start_post_consumer 调用
```

**删除（apps/agent-service/app/）：**

```
chat/safety.py                    # 内容合并进 nodes/safety.py
workers/post_consumer.py          # 替代为 runtime durable consumer
```

**修改（apps/lark-server/）：**

```
src/workers/recall-worker.ts      # max retry 分支补 safety_status="recall_failed" 写入
```

**测试（apps/agent-service/tests/unit/）：**

```
domain/test_safety.py             # Data 类语义测试（4 个）
runtime/test_sink_dispatch.py     # _dispatch_mq_sink 行为
runtime/test_graph.py             # 扩展现有文件，加 Sink.mq 校验测试
data/test_queries.py              # 扩展现有文件，加 get_safety_status 测试
nodes/test_safety.py              # 3 个节点单元测试
chat/test_pre_safety_gate.py      # waiter + run_pre_safety_via_graph 全部退出路径
chat/test_pipeline.py             # 修改现有 pre 路径测试
chat/test_post_actions.py         # 修改现有 _publish_post_check 测试
```

---

## Task 列表概览

```
Phase A: 数据基础层（独立可测）
  Task 1  — Domain Data 类（PreSafetyRequest / PreSafetyVerdict / PostSafetyRequest / Recall）
  Task 2  — data 层 get_safety_status helper

Phase B: Runtime sink dispatch（让后续 wire 不被 compile_graph 拒）
  Task 3  — compile_graph 接受 sink + 启动期 ALL_ROUTES 校验
  Task 4  — emit() sink dispatch 分支 + sink_dispatch.py

Phase C: 节点实现（TDD 红绿循环）
  Task 5  — nodes/safety.py：私有 helper 从 chat/safety.py 搬迁（保留行为，不暴露节点）
  Task 6  — run_post_safety 节点（含业务幂等 + row-missing raise + 短路 + audit + Recall 返回）
  Task 7  — run_pre_safety + resolve_pre_safety_waiter 节点
  Task 8  — pre_safety_gate（waiter + run_pre_safety_via_graph 含 emit_task / completed 标记）

Phase D: Wiring + Lifespan
  Task 9  — wiring/safety.py + placement bind
  Task 10 — main.py lifespan 启动 / 关闭 durable consumer

Phase E: 接入点切换
  Task 11 — chat/post_actions.py 改 emit PostSafetyRequest
  Task 12 — chat/pipeline.py 改用 run_pre_safety_via_graph
  Task 13 — 删除 chat/safety.py + workers/post_consumer.py

Phase F: lark-server
  Task 14 — recall-worker.ts max retry 写 recall_failed

Phase G: 启动 sanity
  Task 15 — compile_graph + start_consumers smoke
```

---

## Task 1 — Domain Data 类

**Files:**
- Create: `apps/agent-service/app/domain/safety.py`
- Test: `apps/agent-service/tests/unit/domain/test_safety.py`

- [ ] **Step 1.1: 写测试**

```python
# apps/agent-service/tests/unit/domain/test_safety.py
"""Tests for safety Data classes (Phase 2)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.safety import (
    PostSafetyRequest,
    PreSafetyRequest,
    PreSafetyVerdict,
    Recall,
)
from app.runtime.data import key_fields


def test_pre_safety_request_is_transient():
    """PreSafetyRequest 是 transient（不落表）。"""
    meta = getattr(PreSafetyRequest, "Meta", None)
    assert meta is not None
    assert getattr(meta, "transient", False) is True


def test_pre_safety_request_key_is_pre_request_id():
    assert key_fields(PreSafetyRequest) == ("pre_request_id",)


def test_pre_safety_verdict_is_transient():
    meta = getattr(PreSafetyVerdict, "Meta", None)
    assert meta is not None
    assert getattr(meta, "transient", False) is True


def test_pre_safety_verdict_key_is_pre_request_id():
    assert key_fields(PreSafetyVerdict) == ("pre_request_id",)


def test_pre_safety_verdict_default_passes():
    """is_blocked 默认 False，block_reason 默认 None。"""
    v = PreSafetyVerdict(pre_request_id="r1", message_id="m1", is_blocked=False)
    assert v.is_blocked is False
    assert v.block_reason is None
    assert v.detail is None


def test_post_safety_request_adopts_agent_responses():
    """PostSafetyRequest 用 adoption mode adopt agent_responses 表。"""
    meta = getattr(PostSafetyRequest, "Meta", None)
    assert meta is not None
    assert getattr(meta, "existing_table", None) == "agent_responses"
    assert getattr(meta, "dedup_column", None) == "session_id"
    # 不能 transient（durable wire 要 row）
    assert getattr(meta, "transient", False) is False


def test_post_safety_request_key_is_session_id():
    assert key_fields(PostSafetyRequest) == ("session_id",)


def test_post_safety_request_required_fields():
    req = PostSafetyRequest(
        session_id="s1",
        trigger_message_id="m1",
        chat_id="c1",
        response_text="hello",
    )
    assert req.session_id == "s1"
    assert req.response_text == "hello"


def test_recall_is_transient():
    meta = getattr(Recall, "Meta", None)
    assert meta is not None
    assert getattr(meta, "transient", False) is True


def test_recall_key_is_session_id():
    assert key_fields(Recall) == ("session_id",)


def test_recall_lane_optional():
    """lane 可选（lark-server recall-worker 从 payload.lane 读，必须支持显式 None / str）。"""
    r = Recall(
        session_id="s1", chat_id="c1", trigger_message_id="m1",
        reason="banned_word",
    )
    assert r.lane is None
    r2 = Recall(
        session_id="s1", chat_id="c1", trigger_message_id="m1",
        reason="banned_word", lane="dev",
    )
    assert r2.lane == "dev"


def test_recall_serialization_matches_legacy_schema():
    """Recall.model_dump 字段集与旧 mq.publish(RECALL,...) 保持一致。"""
    r = Recall(
        session_id="s1", chat_id="c1", trigger_message_id="m1",
        reason="banned_word", detail="hit", lane="dev",
    )
    body = r.model_dump(mode="json")
    assert set(body.keys()) == {
        "session_id", "chat_id", "trigger_message_id",
        "reason", "detail", "lane",
    }


def test_data_class_extra_forbid():
    """Data 类应 frozen=True extra=forbid（pydantic Data base 行为）。"""
    with pytest.raises(ValidationError):
        PreSafetyRequest(
            pre_request_id="r1", message_id="m1",
            message_content="hi", persona_id="p1",
            unknown_field="x",
        )
```

- [ ] **Step 1.2: 跑测试，确认全 fail（导入 app.domain.safety 失败）**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/domain/test_safety.py -v
```

Expected: `ImportError` 或 `ModuleNotFoundError: app.domain.safety`

- [ ] **Step 1.3: 写实现**

```python
# apps/agent-service/app/domain/safety.py
"""Safety Data — Phase 2 dataflow types.

PreSafetyRequest / PreSafetyVerdict 是请求路径内的瞬时控制面数据（transient）；
PostSafetyRequest 用 adoption mode adopt 已有的 ``agent_responses`` 表（lark-server
那边 INSERT 的）；Recall 是出 graph 给 lark-server recall-worker 的事件。
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class PreSafetyRequest(Data):
    """Pre-safety check 请求（chat pipeline 内部触发）。

    pre_request_id 每次 pre-check 独立 uuid4，避免并发 / DLQ replay 时
    waiter Future 互相覆盖。跟 session_id 完全解耦。
    """
    pre_request_id: Annotated[str, Key]
    message_id: str
    message_content: str
    persona_id: str

    class Meta:
        transient = True


class PreSafetyVerdict(Data):
    """Pre-safety check 结果，由 run_pre_safety @node 产出。"""
    pre_request_id: Annotated[str, Key]
    message_id: str
    is_blocked: bool
    block_reason: str | None = None  # BlockReason.value 字符串化
    detail: str | None = None

    class Meta:
        transient = True


class PostSafetyRequest(Data):
    """Post-safety check 请求；adoption mode adopt agent_responses 表。

    Row 由 lark-server 在 chat 完成时已 INSERT；agent-service 仅作为
    durable wire 的入口 trigger。session_id 是 agent_responses 的
    unique business key（无 dedup_hash 列），所以业务幂等通过节点入口
    查 safety_status 短路实现。
    """
    session_id: Annotated[str, Key]
    trigger_message_id: str
    chat_id: str
    response_text: str

    class Meta:
        existing_table = "agent_responses"
        dedup_column = "session_id"


class Recall(Data):
    """撤回事件，通过 Sink.mq("recall") 出 graph 给 lark-server recall-worker。

    payload schema 与旧 ``mq.publish(RECALL, ...)`` 一致；lane 字段
    被 recall-worker.ts 从 payload 直接读取，必须显式带。
    """
    session_id: Annotated[str, Key]
    chat_id: str
    trigger_message_id: str
    reason: str
    detail: str | None = None
    lane: str | None = None

    class Meta:
        transient = True
```

- [ ] **Step 1.4: 跑测试确认全过**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/domain/test_safety.py -v
```

Expected: 11 passed

- [ ] **Step 1.5: Commit**

```bash
git add apps/agent-service/app/domain/safety.py apps/agent-service/tests/unit/domain/test_safety.py
git commit -m "feat(agent-service): add safety domain data classes for phase 2"
```

---

## Task 2 — `get_safety_status` query helper

**Files:**
- Modify: `apps/agent-service/app/data/queries.py` (在 `set_safety_status` 上方加新函数)
- Test: `apps/agent-service/tests/unit/data/test_queries.py` (扩展现有文件)

- [ ] **Step 2.1: 写测试**

在 `apps/agent-service/tests/unit/data/test_queries.py` 末尾追加：

```python
# === get_safety_status ===

import pytest
from sqlalchemy import text

from app.data.queries import get_safety_status, set_safety_status


@pytest.mark.asyncio
async def test_get_safety_status_returns_existing_value(db_session):
    """row 存在 + status 字段有值 → 返回 status 字符串。"""
    await db_session.execute(text(
        "INSERT INTO agent_responses "
        "(id, session_id, trigger_message_id, chat_id, "
        " response_type, replies, agent_metadata, safety_status, status) "
        "VALUES "
        "(gen_random_uuid(), 'sess-get-1', 'msg-1', 'chat-1', "
        " 'reply', '[]'::jsonb, '{}'::jsonb, 'pending', 'created')"
    ))
    await db_session.commit()

    result = await get_safety_status(db_session, "sess-get-1")
    assert result == "pending"


@pytest.mark.asyncio
async def test_get_safety_status_returns_none_when_row_missing(db_session):
    """row 不存在 → 返回 None（不抛）。"""
    result = await get_safety_status(db_session, "sess-does-not-exist")
    assert result is None


@pytest.mark.asyncio
async def test_get_safety_status_after_set(db_session):
    """set_safety_status 后立即 get 应能拿到新值。"""
    await db_session.execute(text(
        "INSERT INTO agent_responses "
        "(id, session_id, trigger_message_id, chat_id, "
        " response_type, replies, agent_metadata, safety_status, status) "
        "VALUES "
        "(gen_random_uuid(), 'sess-rt-1', 'msg-1', 'chat-1', "
        " 'reply', '[]'::jsonb, '{}'::jsonb, 'pending', 'created')"
    ))
    await db_session.commit()

    await set_safety_status(db_session, "sess-rt-1", "passed", {"checked_at": "..."})
    await db_session.commit()
    assert await get_safety_status(db_session, "sess-rt-1") == "passed"
```

注意：如果 `tests/unit/data/test_queries.py` 还没有 `db_session` fixture，参考同目录下现有测试模式，或者从 `tests/conftest.py` 导入。

- [ ] **Step 2.2: 跑测试确认 fail（ImportError on get_safety_status）**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/data/test_queries.py -v -k get_safety_status
```

Expected: ImportError 或 AttributeError

- [ ] **Step 2.3: 在 `apps/agent-service/app/data/queries.py` 的 `set_safety_status` 函数（line 324-344）上方插入：**

```python
async def get_safety_status(
    session: AsyncSession, session_id: str
) -> str | None:
    """Read ``safety_status`` from ``agent_responses``; None if row missing.

    Phase 2 ``run_post_safety`` 节点入口判 None 时 raise（让 durable
    handler 进 DLQ）—— None 不再被当成 fail-open 的 pending 处理，
    见 spec §3.8 / §4.4。
    """
    result = await session.execute(
        text("SELECT safety_status FROM agent_responses WHERE session_id = :sid"),
        {"sid": session_id},
    )
    return result.scalar_one_or_none()
```

- [ ] **Step 2.4: 跑测试确认 pass**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/data/test_queries.py -v -k get_safety_status
```

Expected: 3 passed

- [ ] **Step 2.5: Commit**

```bash
git add apps/agent-service/app/data/queries.py apps/agent-service/tests/unit/data/test_queries.py
git commit -m "feat(agent-service): add get_safety_status query helper"
```

---

## Task 3 — `compile_graph` 放开 sink + 启动期 ALL_ROUTES 校验

**Files:**
- Modify: `apps/agent-service/app/runtime/graph.py` (line 200-218)
- Test: `apps/agent-service/tests/unit/runtime/test_graph.py` (扩展)

- [ ] **Step 3.1: 写测试**

在 `apps/agent-service/tests/unit/runtime/test_graph.py` 末尾追加（如果文件不存在就新建）：

```python
# === Phase 2: Sink dispatch + ALL_ROUTES validation ===

import pytest
from typing import Annotated

from app.runtime import Data, Key, Sink, node, wire
from app.runtime.graph import GraphError, compile_graph
from app.runtime.placement import bind, clear_bindings
from app.runtime.wire import clear_wiring


def _reset():
    clear_wiring()
    clear_bindings()


def test_compile_graph_accepts_wire_with_sink_mq_in_all_routes():
    """``Sink.mq("recall")`` 在 ALL_ROUTES 中应被接受。"""
    _reset()

    class _SinkData(Data):
        session_id: Annotated[str, Key]

    @node
    async def _produce(req: _SinkData) -> _SinkData:
        return req

    wire(_SinkData).to(_produce)
    wire(_SinkData).to(Sink.mq("recall"))  # recall 在 ALL_ROUTES
    bind(_produce).to_app("agent-service")

    # 不应 raise
    g = compile_graph()
    assert any(s.kind == "mq" for w in g.wires for s in w.sinks)


def test_compile_graph_rejects_sink_mq_with_unknown_queue():
    """Sink.mq("not_in_routes") 应在启动期被 compile_graph 拒绝。"""
    _reset()

    class _UnknownData(Data):
        session_id: Annotated[str, Key]

    @node
    async def _produce(req: _UnknownData) -> _UnknownData:
        return req

    wire(_UnknownData).to(_produce)
    wire(_UnknownData).to(Sink.mq("not_in_routes"))
    bind(_produce).to_app("agent-service")

    with pytest.raises(GraphError) as excinfo:
        compile_graph()
    assert "not_in_routes" in str(excinfo.value)
    assert "ALL_ROUTES" in str(excinfo.value)
```

`clear_bindings` 已在 `apps/agent-service/app/runtime/placement.py:25` 定义；`clear_wiring` 在 `app/runtime/wire.py:35`；`reset_emit_runtime` 在 `app/runtime/emit.py:38`。

- [ ] **Step 3.2: 跑测试确认 fail**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/runtime/test_graph.py -v -k "sink"
```

Expected:
- `test_compile_graph_accepts_wire_with_sink_mq_in_all_routes` FAIL（当前 graph.py 把所有 sinks 列为 unimplemented，启动直接 raise）
- `test_compile_graph_rejects_sink_mq_with_unknown_queue` 也 FAIL（错误消息不含 "ALL_ROUTES"，因为现在还是统一 unimplemented 错误）

- [ ] **Step 3.3: 修改 `apps/agent-service/app/runtime/graph.py`**

定位 line 200-218（5 节 unimplemented 检查），原代码：

```python
    # 5) reject edge modifiers whose engine implementation hasn't landed
    # ...
    unimplemented: list[str] = []
    for w in wires:
        if w.debounce is not None:
            unimplemented.append(
                f"wire({w.data_type.__name__}).debounce(...) — not yet "
                f"wired up; the engine has no debounce dispatch and the "
                f"node-signature side (Batched[T]) hasn't been designed"
            )
        if w.sinks:
            kinds = sorted({s.kind for s in w.sinks})
            unimplemented.append(
                f"wire({w.data_type.__name__}).to(Sink.{kinds[0]}(...)) — "
                f"sinks are not dispatched by the engine yet; this surface "
                f"only describes the intended out-of-graph publish"
            )
    if unimplemented:
        raise GraphError(
            "unimplemented wire features:\n  - " + "\n  - ".join(unimplemented)
        )
```

替换为：

```python
    # 5) reject edge modifiers whose engine implementation hasn't landed yet
    # (debounce 还未实现；sink dispatch 在 Phase 2 已实现，校验移到 5b)
    unimplemented: list[str] = []
    for w in wires:
        if w.debounce is not None:
            unimplemented.append(
                f"wire({w.data_type.__name__}).debounce(...) — not yet "
                f"wired up; the engine has no debounce dispatch and the "
                f"node-signature side (Batched[T]) hasn't been designed"
            )
    if unimplemented:
        raise GraphError(
            "unimplemented wire features:\n  - " + "\n  - ".join(unimplemented)
        )

    # 5b) Phase 2 sink dispatch validation: every Sink.mq(name) must
    # reference a queue that is already declared in ALL_ROUTES, otherwise
    # the engine wouldn't know which routing key to use when publishing
    # (lane fan-out + queue->rk binding live there). Catching this at
    # compile time means a typo surfaces at boot, not at the first emit.
    from app.infra.rabbitmq import ALL_ROUTES
    known_queues = {r.queue for r in ALL_ROUTES}
    sink_errors: list[str] = []
    for w in wires:
        for s in w.sinks:
            if s.kind == "mq":
                q = s.params["queue"]
                if q not in known_queues:
                    sink_errors.append(
                        f"wire({w.data_type.__name__}).to(Sink.mq({q!r})): "
                        f"queue not in ALL_ROUTES; sink dispatch needs a "
                        f"registered route to know the routing key. "
                        f"Add Route({q!r}, ...) to ALL_ROUTES first."
                    )
    if sink_errors:
        raise GraphError(
            "sink dispatch validation failed:\n  - " + "\n  - ".join(sink_errors)
        )
```

- [ ] **Step 3.4: 跑测试确认 pass**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/runtime/test_graph.py -v
```

Expected: 全 pass。包含新加的 2 个 sink 测试 + 之前已有的 graph 测试。如果之前已有的 graph 测试因为 `if w.sinks:` 拒绝行为而显式断言"sink raise"，需要相应调整断言（搜索测试代码里 `unimplemented wire features`、`sinks are not dispatched`、`Sink.mq` 等关键字定位）。

- [ ] **Step 3.5: Commit**

```bash
git add apps/agent-service/app/runtime/graph.py apps/agent-service/tests/unit/runtime/test_graph.py
git commit -m "feat(runtime): allow sinks in compile_graph; validate Sink.mq queue against ALL_ROUTES"
```

---

## Task 4 — `emit()` sink dispatch + `sink_dispatch.py`

**Files:**
- Create: `apps/agent-service/app/runtime/sink_dispatch.py`
- Modify: `apps/agent-service/app/runtime/emit.py`
- Test: `apps/agent-service/tests/unit/runtime/test_sink_dispatch.py`

- [ ] **Step 4.1: 写测试**

```python
# apps/agent-service/tests/unit/runtime/test_sink_dispatch.py
"""Tests for Phase 2 Sink dispatch (emit -> mq.publish via SinkSpec)."""
from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime import Data, Key, Sink, node, wire
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.placement import bind, clear_bindings
from app.runtime.wire import clear_wiring


@pytest.fixture(autouse=True)
def _reset_runtime():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


@pytest.mark.asyncio
async def test_emit_dispatches_to_sink_mq_using_route_from_all_routes(monkeypatch):
    """emit Data 触发 wire(Data).to(Sink.mq("recall")) → mq.publish(RECALL, body)。"""
    # 用一个简单 transient Data（避免要 pg 表）
    class _RecallProbe(Data):
        session_id: Annotated[str, Key]
        chat_id: str

        class Meta:
            transient = True

    wire(_RecallProbe).to(Sink.mq("recall"))

    # 设当前 app 为 agent-service（DEFAULT_APP）— mq sink 不需要 placement，
    # 但 compile_graph 要求 wire 至少有一个能跑的端，没 consumer 没问题
    monkeypatch.setenv("APP_NAME", "agent-service")

    fake_publish = AsyncMock()
    with patch("app.runtime.sink_dispatch.mq.publish", fake_publish):
        data = _RecallProbe(session_id="s1", chat_id="c1")
        await emit(data)

    assert fake_publish.await_count == 1
    args, _kwargs = fake_publish.await_args
    route, body = args[0], args[1]
    # route 必须是 ALL_ROUTES 里 queue="recall" 的那一条（routing_key="action.recall"）
    assert route.queue == "recall"
    assert route.rk == "action.recall"
    # body 是 Data.model_dump(mode="json")
    assert body["session_id"] == "s1"
    assert body["chat_id"] == "c1"


@pytest.mark.asyncio
async def test_emit_dispatches_to_sink_alongside_consumer(monkeypatch):
    """同一 Data 上 wire 到 sink 和 consumer，两者都触发。"""
    class _MixData(Data):
        session_id: Annotated[str, Key]

        class Meta:
            transient = True

    consumer_calls: list[_MixData] = []

    @node
    async def _consume(req: _MixData) -> None:
        consumer_calls.append(req)
        return None

    wire(_MixData).to(_consume)
    wire(_MixData).to(Sink.mq("recall"))
    bind(_consume).to_app("agent-service")
    monkeypatch.setenv("APP_NAME", "agent-service")

    fake_publish = AsyncMock()
    with patch("app.runtime.sink_dispatch.mq.publish", fake_publish):
        await emit(_MixData(session_id="s1"))

    assert len(consumer_calls) == 1
    assert fake_publish.await_count == 1


@pytest.mark.asyncio
async def test_route_by_queue_returns_matching_route():
    """_route_by_queue 通过 queue 名查 ALL_ROUTES，找到返回 Route，找不到返回 None。"""
    from app.runtime.sink_dispatch import _route_by_queue

    r = _route_by_queue("recall")
    assert r is not None
    assert r.queue == "recall"
    assert r.rk == "action.recall"

    assert _route_by_queue("not_in_all_routes") is None
```

- [ ] **Step 4.2: 跑测试确认 fail**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/runtime/test_sink_dispatch.py -v
```

Expected: ImportError on `app.runtime.sink_dispatch`

- [ ] **Step 4.3: 写 `apps/agent-service/app/runtime/sink_dispatch.py`**

```python
"""Sink dispatch — emit() -> mq.publish() adapter.

Phase 2 把 ``Sink.mq("queue")`` 真正跑起来：emit 一个 Data 时，对每条
``wire(Data).to(Sink.mq(name))`` 通过 ``ALL_ROUTES`` 查到对应的 ``Route``
(queue + routing_key)，然后用现有 ``mq.publish(route, body)`` 发出去。
``mq.publish`` 内部会按 ``current_lane()`` 做 lane 队列 + routing key
后缀，这部分行为对 sink dispatch 透明。

校验在 ``compile_graph`` 启动期（``app/runtime/graph.py``）做了：找不到
queue 直接 raise GraphError，所以这里 ``_route_by_queue`` 返回 None
是不该发生的事——用 assert 防御就够了。
"""
from __future__ import annotations

from app.infra.rabbitmq import ALL_ROUTES, Route, mq
from app.runtime.data import Data
from app.runtime.sink import SinkSpec


async def _dispatch_mq_sink(sink: SinkSpec, data: Data) -> None:
    queue_name = sink.params["queue"]
    route = _route_by_queue(queue_name)
    assert route is not None, (
        f"compile_graph should have rejected Sink.mq({queue_name!r}) — "
        f"reaching dispatch is a runtime invariant violation"
    )
    body = data.model_dump(mode="json")
    await mq.publish(route, body)


def _route_by_queue(queue_name: str) -> Route | None:
    for r in ALL_ROUTES:
        if r.queue == queue_name:
            return r
    return None
```

- [ ] **Step 4.4: 修改 `apps/agent-service/app/runtime/emit.py`**

定位 wire 循环（line 68-89），原代码：

```python
    for w in graph.wires:
        if w.data_type is not cls:
            continue
        if w.predicate and not w.predicate(data):
            continue
        for c in w.consumers:
            if w.durable:
                # durable: publish to the consumer's queue; the bound
                # worker will consume and run it. No app-side filter.
                from app.runtime.durable import publish_durable

                await publish_durable(w, c, data)
            else:
                # in-process: only run if the consumer is bound to (or
                # falls through to) THIS process's app. Otherwise we'd
                # silently execute a worker-bound @node in the wrong
                # process — bind(...).to_app() would lose its meaning.
                if c not in own_nodes:
                    continue
                kwargs = await _resolve_inputs(c, data, w)
                await c(**kwargs)
```

在 consumer 循环之后（同一个 `for w in graph.wires:` 体内）增加 sink dispatch 分支：

```python
    for w in graph.wires:
        if w.data_type is not cls:
            continue
        if w.predicate and not w.predicate(data):
            continue
        for c in w.consumers:
            if w.durable:
                from app.runtime.durable import publish_durable

                await publish_durable(w, c, data)
            else:
                if c not in own_nodes:
                    continue
                kwargs = await _resolve_inputs(c, data, w)
                await c(**kwargs)
        # Phase 2: sink dispatch — out-of-graph publish (RabbitMQ).
        # compile_graph 已校验 Sink.mq(name) ∈ ALL_ROUTES，这里直接调。
        for s in w.sinks:
            if s.kind == "mq":
                from app.runtime.sink_dispatch import _dispatch_mq_sink
                await _dispatch_mq_sink(s, data)
```

- [ ] **Step 4.5: 跑测试确认 pass**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/runtime/test_sink_dispatch.py -v
```

Expected: 3 passed

- [ ] **Step 4.6: 跑全 runtime 测试做 regression check**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/runtime/ -v
```

Expected: 全 pass（包括 vectorize / memory_vectorize 的现有 wire 测试）

- [ ] **Step 4.7: Commit**

```bash
git add apps/agent-service/app/runtime/sink_dispatch.py apps/agent-service/app/runtime/emit.py apps/agent-service/tests/unit/runtime/test_sink_dispatch.py
git commit -m "feat(runtime): implement Sink.mq dispatch via ALL_ROUTES lookup"
```

---

## Task 5 — `nodes/safety.py`：搬迁 helper（不引入节点）

**目的**：先把 `chat/safety.py` 里的私有 helper（4 个 LLM 检查 + banned word + BlockReason + _GUARD_*）原样搬到 `nodes/safety.py` module-level，并加一个 `_run_audit` 包 banned word + LLM output。这一步**不引入节点**，是行为保留迁移。

**Files:**
- Create: `apps/agent-service/app/nodes/safety.py`
- Test: `apps/agent-service/tests/unit/nodes/test_safety.py` (一个 import 烟囱测试)

- [ ] **Step 5.1: 写一个 import smoke test**

```python
# apps/agent-service/tests/unit/nodes/test_safety.py
"""Tests for nodes/safety.py (Phase 2)."""
from __future__ import annotations


def test_module_imports():
    """烟囱测试：模块能加载，含必要 helper / 常量。"""
    from app.nodes import safety as m

    assert hasattr(m, "_check_banned_word")
    assert hasattr(m, "_check_injection")
    assert hasattr(m, "_check_politics")
    assert hasattr(m, "_check_nsfw")
    assert hasattr(m, "_check_output")
    assert hasattr(m, "_run_audit")
    assert hasattr(m, "BlockReason")
    assert hasattr(m, "TERMINAL_STATUSES")
    # TERMINAL_STATUSES 内容
    assert m.TERMINAL_STATUSES == frozenset(
        {"passed", "blocked", "recalled", "recall_failed"}
    )
```

- [ ] **Step 5.2: 跑测试确认 fail**

```
cd apps/agent-service && uv run pytest tests/unit/nodes/test_safety.py::test_module_imports -v
```

Expected: ImportError

- [ ] **Step 5.3: 写 `apps/agent-service/app/nodes/safety.py`（搬迁现有 chat/safety.py 的内容 + 新加 _run_audit + TERMINAL_STATUSES，不写节点本体）**

```python
"""Safety pipeline @nodes + private helpers (Phase 2).

合并 ``app/chat/safety.py`` 和 post safety chain 的所有逻辑：
- module-level 私有 helpers：banned word + 4 个 LLM 检查 + ``_run_audit``
- module-level enum / config：``BlockReason`` / ``_GUARD_*``
- @node：``run_pre_safety`` / ``resolve_pre_safety_waiter`` / ``run_post_safety``
- 常量：``TERMINAL_STATUSES``

节点 / wiring / 外部入口由后续 Task 6-9 添加；本 Task 只搬迁 helper 保留行为。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from app.agent.core import Agent, AgentConfig
from app.infra.redis import get_redis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Post-safety 节点入口的"已完成"短路集合（Phase 2 §3.2 / §4.4）。
# - passed / blocked: agent-service 写的（"blocked" 是迁移期遗留瞬态）
# - recalled / recall_failed: lark-server recall-worker 写的终态
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"passed", "blocked", "recalled", "recall_failed"}
)

# Redis key for banned words set
_BANNED_WORDS_KEY = "banned_words"

# Personas that block NSFW content (minors)
_NSFW_BLOCKED_PERSONAS = frozenset({"ayana"})

# Pre/Post check 用的 4 个 guard agent
_GUARD_INJECTION = AgentConfig(
    "guard_prompt_injection", "guard-model", "pre-injection-check"
)
_GUARD_POLITICS = AgentConfig(
    "guard_sensitive_politics", "guard-model", "pre-politics-check"
)
_GUARD_NSFW = AgentConfig("guard_nsfw_content", "guard-model", "pre-nsfw-check")
_GUARD_OUTPUT = AgentConfig("guard_output_safety", "guard-model", "post-safety-check")


# ---------------------------------------------------------------------------
# Block reason enum
# ---------------------------------------------------------------------------


class BlockReason(StrEnum):
    BANNED_WORD = "banned_word"
    PROMPT_INJECTION = "prompt_injection"
    SENSITIVE_POLITICS = "sensitive_politics"
    NSFW_CONTENT = "nsfw_content"


# ---------------------------------------------------------------------------
# Internal result dataclasses (used between helpers and nodes; not exported)
# ---------------------------------------------------------------------------


@dataclass
class _PreCheckOutcome:
    is_blocked: bool = False
    block_reason: BlockReason | None = None
    detail: str | None = None


@dataclass
class _PostAuditOutcome:
    is_blocked: bool = False
    reason: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Structured output schemas for LLM checks
# ---------------------------------------------------------------------------


class _InjectionResult(BaseModel):
    is_injection: bool = Field(description="Is this a prompt injection attempt")
    confidence: float = Field(ge=0, le=1)


class _PoliticsResult(BaseModel):
    is_sensitive: bool = Field(description="Involves sensitive political topics")
    confidence: float = Field(ge=0, le=1)


class _NsfwResult(BaseModel):
    is_nsfw: bool = Field(description="Contains NSFW / adult content")
    confidence: float = Field(ge=0, le=1)


class _OutputSafetyResult(BaseModel):
    is_unsafe: bool = Field(description="Response contains unsafe content")
    confidence: float = Field(ge=0, le=1)


# ---------------------------------------------------------------------------
# Banned word check (shared by pre and post)
# ---------------------------------------------------------------------------


async def _check_banned_word(text: str) -> str | None:
    """Return the matched banned word, or None if clean."""
    redis = await get_redis()
    banned_words = await redis.smembers(_BANNED_WORDS_KEY)
    if not banned_words:
        return None
    normalized = text.replace(" ", "").lower()
    for word in banned_words:
        if word in normalized:
            return word
    return None


# ---------------------------------------------------------------------------
# Individual pre-check functions
# ---------------------------------------------------------------------------


async def _check_injection(message: str) -> _PreCheckOutcome:
    try:
        result: _InjectionResult = await Agent(
            _GUARD_INJECTION,
            model_kwargs={"reasoning_effort": "low"},
            update_trace=False,
        ).extract(_InjectionResult, messages=[], prompt_vars={"message": message})
        if result.is_injection and result.confidence >= 0.85:
            logger.warning(
                "Prompt injection detected: confidence=%.2f", result.confidence
            )
            return _PreCheckOutcome(
                is_blocked=True,
                block_reason=BlockReason.PROMPT_INJECTION,
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Injection check failed: %s", e)
    return _PreCheckOutcome()


async def _check_politics(message: str) -> _PreCheckOutcome:
    try:
        result: _PoliticsResult = await Agent(
            _GUARD_POLITICS,
            model_kwargs={"reasoning_effort": "low"},
            update_trace=False,
        ).extract(_PoliticsResult, messages=[], prompt_vars={"message": message})
        if result.is_sensitive and result.confidence >= 0.85:
            logger.warning(
                "Sensitive politics detected: confidence=%.2f", result.confidence
            )
            return _PreCheckOutcome(
                is_blocked=True,
                block_reason=BlockReason.SENSITIVE_POLITICS,
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Politics check failed: %s", e)
    return _PreCheckOutcome()


async def _check_nsfw(message: str, persona_id: str) -> _PreCheckOutcome:
    try:
        result: _NsfwResult = await Agent(
            _GUARD_NSFW,
            model_kwargs={"reasoning_effort": "low"},
            update_trace=False,
        ).extract(_NsfwResult, messages=[], prompt_vars={"message": message})
        if result.is_nsfw and result.confidence >= 0.75:
            if persona_id in _NSFW_BLOCKED_PERSONAS:
                logger.warning(
                    "NSFW blocked: persona=%s, confidence=%.2f",
                    persona_id,
                    result.confidence,
                )
                return _PreCheckOutcome(
                    is_blocked=True,
                    block_reason=BlockReason.NSFW_CONTENT,
                    detail=f"confidence={result.confidence}",
                )
            logger.info(
                "NSFW logged (pass): persona=%s, confidence=%.2f",
                persona_id,
                result.confidence,
            )
    except Exception as e:
        logger.error("NSFW check failed: %s", e)
    return _PreCheckOutcome()


async def _run_pre_audit(
    message_content: str, persona_id: str
) -> _PreCheckOutcome:
    """跑 4 个 pre-check（banned word + 3 个 LLM 并行），20s 超时 fail-open。

    跟旧 ``app/chat/safety.py:run_pre_check`` 行为一致。
    """
    # Fast path: banned word
    try:
        banned = await _check_banned_word(message_content)
        if banned:
            logger.warning("Banned word hit: %s", banned)
            return _PreCheckOutcome(
                is_blocked=True,
                block_reason=BlockReason.BANNED_WORD,
                detail=banned,
            )
    except Exception as e:
        logger.error("Banned word check failed: %s", e)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                _check_injection(message_content),
                _check_politics(message_content),
                _check_nsfw(message_content, persona_id),
                return_exceptions=True,
            ),
            timeout=20.0,
        )
    except TimeoutError:
        logger.warning("Pre-check exceeded 20s, passing through")
        return _PreCheckOutcome()

    for r in results:
        if isinstance(r, _PreCheckOutcome) and r.is_blocked:
            return r
        if isinstance(r, Exception):
            logger.error("Pre-check sub-task failed: %s", r)

    return _PreCheckOutcome()


# ---------------------------------------------------------------------------
# Post-check helpers
# ---------------------------------------------------------------------------


async def _check_output(response_text: str) -> _PostAuditOutcome:
    """LLM output safety audit。"""
    try:
        result: _OutputSafetyResult = await Agent(
            _GUARD_OUTPUT,
            model_kwargs={"reasoning_effort": "low"},
            update_trace=False,
        ).extract(
            _OutputSafetyResult, messages=[], prompt_vars={"response": response_text}
        )
        if result.is_unsafe and result.confidence >= 0.7:
            logger.warning("Output unsafe: confidence=%.2f", result.confidence)
            return _PostAuditOutcome(
                is_blocked=True,
                reason="output_unsafe",
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Output safety LLM check failed: %s", e)
    return _PostAuditOutcome()


async def _run_audit(response_text: str) -> _PostAuditOutcome:
    """跑 banned word + LLM output audit；fail-open（跟旧 run_post_check 一致）。"""
    if not response_text or not response_text.strip():
        return _PostAuditOutcome()

    # Step 1: banned word
    try:
        banned = await _check_banned_word(response_text)
        if banned:
            logger.warning("Output banned word hit: %s", banned)
            return _PostAuditOutcome(
                is_blocked=True, reason="output_banned_word", detail=banned
            )
    except Exception as e:
        logger.error("Output banned word check failed: %s", e)

    # Step 2: LLM audit
    return await _check_output(response_text)
```

- [ ] **Step 5.4: 跑测试确认 pass**

Run:
```
cd apps/agent-service && uv run pytest tests/unit/nodes/test_safety.py::test_module_imports -v
```

Expected: 1 passed

- [ ] **Step 5.5: Commit**

```bash
git add apps/agent-service/app/nodes/safety.py apps/agent-service/tests/unit/nodes/test_safety.py
git commit -m "feat(agent-service): scaffold nodes/safety.py with private helpers and TERMINAL_STATUSES"
```

---

## Task 6 — `run_post_safety` 节点

**Files:**
- Modify: `apps/agent-service/app/nodes/safety.py` (在文件末尾追加节点定义)
- Test: `apps/agent-service/tests/unit/nodes/test_safety.py` (扩展)

- [ ] **Step 6.1: 写测试**

在 `apps/agent-service/tests/unit/nodes/test_safety.py` 末尾追加：

```python
# === run_post_safety ===

import pytest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.safety import PostSafetyRequest, Recall


def _make_req(session_id="sess-1") -> PostSafetyRequest:
    return PostSafetyRequest(
        session_id=session_id,
        trigger_message_id="msg-1",
        chat_id="chat-1",
        response_text="hello world",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["passed", "blocked", "recalled", "recall_failed"])
async def test_run_post_safety_short_circuits_on_terminal_status(status):
    """terminal 状态下短路 return None，不调 _run_audit / set_safety_status。"""
    from app.nodes import safety as m

    req = _make_req()
    fake_get = AsyncMock(return_value=status)
    fake_audit = AsyncMock()
    fake_set = AsyncMock()
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None
    fake_get_session = MagicMock(return_value=fake_session)

    with (
        patch.object(m, "get_safety_status", fake_get),
        patch.object(m, "_run_audit", fake_audit),
        patch.object(m, "set_safety_status", fake_set),
        patch.object(m, "get_session", fake_get_session),
    ):
        result = await m.run_post_safety(req)

    assert result is None
    fake_audit.assert_not_called()
    fake_set.assert_not_called()


@pytest.mark.asyncio
async def test_run_post_safety_raises_when_row_missing():
    """row 不存在 → raise RuntimeError → durable handler 进 DLQ。"""
    from app.nodes import safety as m

    fake_get = AsyncMock(return_value=None)
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None
    fake_get_session = MagicMock(return_value=fake_session)

    with (
        patch.object(m, "get_safety_status", fake_get),
        patch.object(m, "get_session", fake_get_session),
    ):
        with pytest.raises(RuntimeError) as excinfo:
            await m.run_post_safety(_make_req("missing-row"))
    assert "missing-row" in str(excinfo.value)
    assert "lark-server" in str(excinfo.value)


@pytest.mark.asyncio
async def test_run_post_safety_passed_writes_status_and_returns_none():
    """audit pass → set_safety_status('passed', ...) + return None（不产 Recall）。"""
    from app.nodes import safety as m

    fake_get = AsyncMock(return_value="pending")
    fake_audit = AsyncMock(return_value=m._PostAuditOutcome(is_blocked=False))
    fake_set = AsyncMock()
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None
    fake_get_session = MagicMock(return_value=fake_session)

    with (
        patch.object(m, "get_safety_status", fake_get),
        patch.object(m, "_run_audit", fake_audit),
        patch.object(m, "set_safety_status", fake_set),
        patch.object(m, "get_session", fake_get_session),
    ):
        result = await m.run_post_safety(_make_req("sess-pass"))

    assert result is None
    fake_set.assert_awaited_once()
    args = fake_set.await_args.args
    assert args[1] == "sess-pass"
    assert args[2] == "passed"


@pytest.mark.asyncio
async def test_run_post_safety_blocked_returns_recall_without_writing_status():
    """audit blocked → return Recall，不调 set_safety_status（recall-worker 写终态）。"""
    from app.nodes import safety as m

    fake_get = AsyncMock(return_value="pending")
    fake_audit = AsyncMock(
        return_value=m._PostAuditOutcome(
            is_blocked=True, reason="output_unsafe", detail="confidence=0.9"
        )
    )
    fake_set = AsyncMock()
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None
    fake_get_session = MagicMock(return_value=fake_session)

    with (
        patch.object(m, "get_safety_status", fake_get),
        patch.object(m, "_run_audit", fake_audit),
        patch.object(m, "set_safety_status", fake_set),
        patch.object(m, "get_session", fake_get_session),
        patch.object(m, "get_lane", MagicMock(return_value="dev")),
    ):
        result = await m.run_post_safety(_make_req("sess-block"))

    assert isinstance(result, Recall)
    assert result.session_id == "sess-block"
    assert result.reason == "output_unsafe"
    assert result.detail == "confidence=0.9"
    assert result.lane == "dev"
    fake_set.assert_not_called()
```

- [ ] **Step 6.2: 跑测试确认 fail**

```
cd apps/agent-service && uv run pytest tests/unit/nodes/test_safety.py -v -k post
```

Expected: AttributeError: module 'app.nodes.safety' has no attribute 'run_post_safety'

- [ ] **Step 6.3: 在 `apps/agent-service/app/nodes/safety.py` 末尾追加：**

```python
# ---------------------------------------------------------------------------
# Public @node entries
# ---------------------------------------------------------------------------

from datetime import UTC, datetime

from app.api.middleware import get_lane
from app.data.queries import get_safety_status, set_safety_status
from app.data.session import get_session
from app.domain.safety import (
    PostSafetyRequest,
    PreSafetyRequest,
    PreSafetyVerdict,
    Recall,
)
from app.runtime import node


@node
async def run_post_safety(req: PostSafetyRequest) -> Recall | None:
    """Audit + 决定是否撤回，单节点完成（Phase 2 §3.2）。

    幂等用 ``safety_status`` 短路：
      - row 不存在 → raise → DLQ（lark-server INSERT 链路问题）
      - 已 ``TERMINAL_STATUSES``（passed/blocked/recalled/recall_failed） → return None
      - pending → 跑 audit；blocked 路径 return Recall（@node 自动 emit -> sink），
        passed 路径写 status="passed"
    blocked 路径**不写 status**——recall-worker 会写最终 recalled / recall_failed，
    避免 race（spec §3.2）。
    """
    async with get_session() as s:
        current = await get_safety_status(s, req.session_id)
    if current is None:
        raise RuntimeError(
            f"agent_responses row missing for session_id={req.session_id}; "
            f"lark-server must INSERT before agent-service emits "
            f"PostSafetyRequest"
        )
    if current in TERMINAL_STATUSES:
        logger.info(
            "post safety short-circuit: session_id=%s already %s",
            req.session_id, current,
        )
        return None

    decision = await _run_audit(req.response_text)
    checked_at = datetime.now(UTC).isoformat()

    if decision.is_blocked:
        return Recall(
            session_id=req.session_id,
            chat_id=req.chat_id,
            trigger_message_id=req.trigger_message_id,
            reason=decision.reason or "unknown",
            detail=decision.detail,
            lane=get_lane(),
        )

    async with get_session() as s:
        await set_safety_status(
            s, req.session_id, "passed", {"checked_at": checked_at}
        )
    return None
```

- [ ] **Step 6.4: 跑测试确认 pass**

```
cd apps/agent-service && uv run pytest tests/unit/nodes/test_safety.py -v -k post
```

Expected: 7 passed (4 short-circuit cases + row-missing + passed + blocked)

- [ ] **Step 6.5: Commit**

```bash
git add apps/agent-service/app/nodes/safety.py apps/agent-service/tests/unit/nodes/test_safety.py
git commit -m "feat(agent-service): add run_post_safety @node with business idempotency"
```

---

## Task 7 — `run_pre_safety` + `resolve_pre_safety_waiter` 节点

**Files:**
- Modify: `apps/agent-service/app/nodes/safety.py` (追加 2 个节点)
- Test: `apps/agent-service/tests/unit/nodes/test_safety.py` (扩展)

- [ ] **Step 7.1: 写测试**

在 `apps/agent-service/tests/unit/nodes/test_safety.py` 末尾追加：

```python
# === run_pre_safety + resolve_pre_safety_waiter ===

from app.domain.safety import PreSafetyRequest, PreSafetyVerdict


@pytest.mark.asyncio
async def test_run_pre_safety_returns_pass_verdict_when_clean():
    """所有检查通过 → is_blocked=False。"""
    from app.nodes import safety as m

    fake_audit = AsyncMock(return_value=m._PreCheckOutcome(is_blocked=False))
    req = PreSafetyRequest(
        pre_request_id="pr-1", message_id="m-1",
        message_content="hello", persona_id="ayana",
    )
    with patch.object(m, "_run_pre_audit", fake_audit):
        verdict = await m.run_pre_safety(req)

    assert isinstance(verdict, PreSafetyVerdict)
    assert verdict.pre_request_id == "pr-1"
    assert verdict.is_blocked is False
    assert verdict.block_reason is None


@pytest.mark.asyncio
async def test_run_pre_safety_returns_block_verdict_with_reason():
    """audit 返回 blocked → verdict 字段映射正确。"""
    from app.nodes import safety as m

    outcome = m._PreCheckOutcome(
        is_blocked=True,
        block_reason=m.BlockReason.PROMPT_INJECTION,
        detail="confidence=0.9",
    )
    fake_audit = AsyncMock(return_value=outcome)
    req = PreSafetyRequest(
        pre_request_id="pr-2", message_id="m-1",
        message_content="ignore previous", persona_id="ayana",
    )
    with patch.object(m, "_run_pre_audit", fake_audit):
        verdict = await m.run_pre_safety(req)

    assert verdict.is_blocked is True
    assert verdict.block_reason == "prompt_injection"
    assert verdict.detail == "confidence=0.9"


@pytest.mark.asyncio
async def test_resolve_pre_safety_waiter_calls_gate_resolve():
    """节点 body 把 verdict 塞回本进程 pre_safety_gate.resolve。"""
    from app.nodes import safety as m

    verdict = PreSafetyVerdict(
        pre_request_id="pr-3", message_id="m-1", is_blocked=False
    )
    fake_resolve = MagicMock()
    with patch("app.chat.pre_safety_gate.resolve", fake_resolve):
        result = await m.resolve_pre_safety_waiter(verdict)

    assert result is None
    fake_resolve.assert_called_once_with(verdict)
```

- [ ] **Step 7.2: 跑测试确认 fail**

```
cd apps/agent-service && uv run pytest tests/unit/nodes/test_safety.py -v -k "pre"
```

Expected: AttributeError: 'run_pre_safety' / 'resolve_pre_safety_waiter' not in module；以及 ImportError on `app.chat.pre_safety_gate`（Task 8 才创建，这里允许 patch 失败先把节点本体写完）

- [ ] **Step 7.3: 在 `apps/agent-service/app/nodes/safety.py` 末尾追加：**

```python
@node
async def run_pre_safety(req: PreSafetyRequest) -> PreSafetyVerdict:
    """跑 4 个并行 pre-check，返回 verdict。

    内部调 ``_run_pre_audit`` 复用 banned word + 3 个 LLM 检查；
    fail-open 已在 audit 内部处理（超时 / 异常 → 通过 verdict）。
    """
    outcome = await _run_pre_audit(req.message_content, req.persona_id)
    return PreSafetyVerdict(
        pre_request_id=req.pre_request_id,
        message_id=req.message_id,
        is_blocked=outcome.is_blocked,
        block_reason=str(outcome.block_reason) if outcome.block_reason else None,
        detail=outcome.detail,
    )


@node
async def resolve_pre_safety_waiter(verdict: PreSafetyVerdict) -> None:
    """收尾节点：把 verdict 塞回本进程的 Future registry。"""
    # 延迟 import 避免循环依赖（pre_safety_gate import nodes.safety 反过来）
    from app.chat import pre_safety_gate
    pre_safety_gate.resolve(verdict)
    return None
```

- [ ] **Step 7.4: 跑测试确认 pass**

```
cd apps/agent-service && uv run pytest tests/unit/nodes/test_safety.py -v -k "pre"
```

注意：`test_resolve_pre_safety_waiter_calls_gate_resolve` 会因为 `app.chat.pre_safety_gate` 还没创建而失败。先跳过它，等 Task 8 完成后再跑。

可以临时 mark：

```python
@pytest.mark.skip(reason="awaits Task 8 (app.chat.pre_safety_gate)")
@pytest.mark.asyncio
async def test_resolve_pre_safety_waiter_calls_gate_resolve():
    ...
```

或者直接确认前两个测试 pass 就 commit，Task 8 完成后回过头跑全测试。

Expected: 2 passed, 1 skipped

- [ ] **Step 7.5: Commit**

```bash
git add apps/agent-service/app/nodes/safety.py apps/agent-service/tests/unit/nodes/test_safety.py
git commit -m "feat(agent-service): add run_pre_safety and resolve_pre_safety_waiter @nodes"
```

---

## Task 8 — `pre_safety_gate`（waiter + run_pre_safety_via_graph）

**Files:**
- Create: `apps/agent-service/app/chat/pre_safety_gate.py`
- Test: `apps/agent-service/tests/unit/chat/test_pre_safety_gate.py`

- [ ] **Step 8.1: 写测试**

```python
# apps/agent-service/tests/unit/chat/test_pre_safety_gate.py
"""Tests for chat/pre_safety_gate.py (Phase 2)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.safety import PreSafetyVerdict


@pytest.mark.asyncio
async def test_register_creates_future_and_resolve_sets_result():
    from app.chat import pre_safety_gate

    fut = pre_safety_gate.register("pr-1")
    assert isinstance(fut, asyncio.Future)
    assert not fut.done()

    verdict = PreSafetyVerdict(
        pre_request_id="pr-1", message_id="m-1", is_blocked=False
    )
    pre_safety_gate.resolve(verdict)
    assert fut.done()
    assert fut.result() is verdict
    pre_safety_gate.cleanup("pr-1")


@pytest.mark.asyncio
async def test_resolve_for_unknown_request_id_is_noop():
    """resolve 对未 register 的 id 不抛异常。"""
    from app.chat import pre_safety_gate

    verdict = PreSafetyVerdict(
        pre_request_id="ghost", message_id="m-1", is_blocked=False
    )
    pre_safety_gate.resolve(verdict)  # 不抛


@pytest.mark.asyncio
async def test_resolve_for_already_done_future_is_noop():
    """resolve 对已 done 的 future 不抛 InvalidStateError。"""
    from app.chat import pre_safety_gate

    fut = pre_safety_gate.register("pr-2")
    fut.cancel()
    await asyncio.sleep(0)

    verdict = PreSafetyVerdict(
        pre_request_id="pr-2", message_id="m-1", is_blocked=False
    )
    pre_safety_gate.resolve(verdict)  # 不抛
    pre_safety_gate.cleanup("pr-2")


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_returns_verdict_on_normal_completion():
    """正常路径：emit 触发节点链路 → verdict 出现 → 返回 verdict。"""
    from app.chat import pre_safety_gate

    captured_pre_request_id: list[str] = []

    async def fake_emit(req):
        captured_pre_request_id.append(req.pre_request_id)
        # 模拟 graph 链路完成：直接 resolve
        verdict = PreSafetyVerdict(
            pre_request_id=req.pre_request_id,
            message_id=req.message_id,
            is_blocked=False,
        )
        pre_safety_gate.resolve(verdict)

    with patch("app.chat.pre_safety_gate.emit", fake_emit):
        v = await pre_safety_gate.run_pre_safety_via_graph(
            message_id="m-1", content="hi", persona_id="ayana"
        )

    assert isinstance(v, PreSafetyVerdict)
    assert v.is_blocked is False
    assert v.message_id == "m-1"
    assert captured_pre_request_id  # 用了一个 uuid4 pre_request_id


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_fails_open_on_timeout():
    """节点卡住 21s+ → fail-open + emit_task 被 cancel。"""
    from app.chat import pre_safety_gate

    cancelled = asyncio.Event()

    async def fake_emit(req):
        try:
            await asyncio.sleep(60)  # 卡住
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with (
        patch("app.chat.pre_safety_gate.emit", fake_emit),
        patch("app.chat.pre_safety_gate._PRE_SAFETY_TIMEOUT_SECONDS", 0.05),
    ):
        v = await pre_safety_gate.run_pre_safety_via_graph(
            message_id="m-1", content="hi", persona_id="ayana"
        )

    assert v.is_blocked is False  # fail-open
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_fails_open_on_emit_error():
    """emit 自身抛异常 → fail-open 立即返回，不等满 timeout。"""
    from app.chat import pre_safety_gate

    async def fake_emit(req):
        raise RuntimeError("mq not connected")

    with patch("app.chat.pre_safety_gate.emit", fake_emit):
        v = await pre_safety_gate.run_pre_safety_via_graph(
            message_id="m-1", content="hi", persona_id="ayana"
        )

    assert v.is_blocked is False


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_caller_cancel_cancels_emit():
    """外层调用方 cancel → emit_task 也被 cancel + waiter cleanup（reviewer round 6）。"""
    from app.chat import pre_safety_gate

    emit_started = asyncio.Event()
    emit_cancelled = asyncio.Event()

    async def fake_emit(req):
        emit_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            emit_cancelled.set()
            raise

    async def caller():
        with patch("app.chat.pre_safety_gate.emit", fake_emit):
            await pre_safety_gate.run_pre_safety_via_graph(
                message_id="m-1", content="hi", persona_id="ayana"
            )

    task = asyncio.create_task(caller())
    await emit_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert emit_cancelled.is_set()


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_concurrent_uses_independent_ids():
    """并发多次调用每次独立 pre_request_id；不串。"""
    from app.chat import pre_safety_gate

    seen_ids: list[str] = []

    async def fake_emit(req):
        seen_ids.append(req.pre_request_id)
        verdict = PreSafetyVerdict(
            pre_request_id=req.pre_request_id,
            message_id=req.message_id,
            is_blocked=False,
        )
        pre_safety_gate.resolve(verdict)

    with patch("app.chat.pre_safety_gate.emit", fake_emit):
        results = await asyncio.gather(*[
            pre_safety_gate.run_pre_safety_via_graph(
                message_id=f"m-{i}", content="hi", persona_id="ayana"
            )
            for i in range(5)
        ])

    assert len(results) == 5
    assert all(v.is_blocked is False for v in results)
    assert len(set(seen_ids)) == 5  # 都是不同 uuid
```

- [ ] **Step 8.2: 跑测试确认 fail**

```
cd apps/agent-service && uv run pytest tests/unit/chat/test_pre_safety_gate.py -v
```

Expected: ImportError on `app.chat.pre_safety_gate`

- [ ] **Step 8.3: 写实现 `apps/agent-service/app/chat/pre_safety_gate.py`**

```python
"""Pre-safety chat gate — local Future waiter + run_pre_safety_via_graph.

Phase 2 §3.4：chat pipeline 通过这个 module 把 pre-check 控制面接进 graph。
``run_pre_safety_via_graph`` 是给 chat pipeline 调的统一入口；返回 verdict
是 ``PreSafetyVerdict``，跟 ``_buffer_until_pre`` 的 race 模型对齐。

实现要点：
1. ``emit()`` in-process 是同步 await 整链路（节点 -> 装饰器自动 emit verdict
   -> resolve_pre_safety_waiter -> set future）。节点卡住时 ``await emit`` 也卡住，
   所以必须把 emit 包成独立 task，让超时检查跑在调用方协程上。
2. 用 ``asyncio.wait({fut, emit_task}, FIRST_COMPLETED)`` —— emit_task 早失败
   立即 fail-open，fut 先完成直接拿 verdict。
3. ``completed`` 标记区分 3 条退出：
   - 拿到 verdict 才置 True（finally 不 cancel emit_task）
   - timeout / emit 早失败 / 外层 cancel：completed=False，finally 一律
     cancel emit_task + suppress CancelledError；CancelledError 再
     propagate 给外层
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress

from app.domain.safety import PreSafetyRequest, PreSafetyVerdict
from app.runtime.emit import emit

logger = logging.getLogger(__name__)

# 节点内部超时 20s（``_run_pre_audit`` ceiling），加 1s 缓冲
_PRE_SAFETY_TIMEOUT_SECONDS: float = 21.0

_waiters: dict[str, asyncio.Future[PreSafetyVerdict]] = {}


def register(pre_request_id: str) -> asyncio.Future[PreSafetyVerdict]:
    fut: asyncio.Future[PreSafetyVerdict] = (
        asyncio.get_running_loop().create_future()
    )
    _waiters[pre_request_id] = fut
    return fut


def resolve(verdict: PreSafetyVerdict) -> None:
    fut = _waiters.get(verdict.pre_request_id)
    if fut is None or fut.done():
        return  # caller 已超时清理 / 不存在 / 已 cancel —— 安全无操作
    fut.set_result(verdict)


def cleanup(pre_request_id: str) -> None:
    _waiters.pop(pre_request_id, None)


async def run_pre_safety_via_graph(
    message_id: str, content: str, persona_id: str
) -> PreSafetyVerdict:
    """Chat pipeline 调入口：emit + 等 verdict + fail-open。"""
    pre_request_id = str(uuid.uuid4())
    fut = register(pre_request_id)
    emit_task: asyncio.Task = asyncio.create_task(
        emit(PreSafetyRequest(
            pre_request_id=pre_request_id,
            message_id=message_id,
            message_content=content,
            persona_id=persona_id,
        ))
    )

    completed = False
    try:
        done, _pending = await asyncio.wait(
            {fut, emit_task},
            timeout=_PRE_SAFETY_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            logger.warning("pre safety timeout: pre_request_id=%s", pre_request_id)
        elif fut in done:
            if not emit_task.done():
                await emit_task
            elif emit_task.exception():
                logger.error(
                    "pre safety emit failed after verdict: pre_request_id=%s, error=%s",
                    pre_request_id, emit_task.exception(),
                )
            result = fut.result()
            completed = True
            return result
        else:
            assert emit_task.done()
            logger.warning(
                "pre safety emit failed before verdict: pre_request_id=%s, error=%s",
                pre_request_id, emit_task.exception(),
            )
    finally:
        if not completed and not emit_task.done():
            emit_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await emit_task
        cleanup(pre_request_id)

    return PreSafetyVerdict(
        pre_request_id=pre_request_id, message_id=message_id, is_blocked=False
    )
```

- [ ] **Step 8.4: 跑测试确认 pass**

```
cd apps/agent-service && uv run pytest tests/unit/chat/test_pre_safety_gate.py -v
```

Expected: 8 passed

- [ ] **Step 8.5: 回头跑 Task 7 跳过的测试**

如果 Task 7 用 `@pytest.mark.skip` 先跳过了 `test_resolve_pre_safety_waiter_calls_gate_resolve`，把 skip mark 删掉再跑：

```
cd apps/agent-service && uv run pytest tests/unit/nodes/test_safety.py -v -k resolve
```

Expected: 1 passed

- [ ] **Step 8.6: Commit**

```bash
git add apps/agent-service/app/chat/pre_safety_gate.py apps/agent-service/tests/unit/chat/test_pre_safety_gate.py apps/agent-service/tests/unit/nodes/test_safety.py
git commit -m "feat(agent-service): add pre_safety_gate with completed-flag task lifecycle"
```

---

## Task 9 — Wiring + placement bind

**Files:**
- Create: `apps/agent-service/app/wiring/safety.py`
- Modify: `apps/agent-service/app/wiring/__init__.py` (import 新 module)
- Test: `apps/agent-service/tests/unit/runtime/test_wiring.py` 或新建 `test_safety_wiring.py`

- [ ] **Step 9.1: 写测试**

```python
# apps/agent-service/tests/unit/wiring/test_safety_wiring.py
"""Tests for wiring/safety.py — verify 4 wires + bind correctness."""
from __future__ import annotations


def test_safety_wiring_registers_expected_wires_and_bindings():
    """import wiring/safety 后，WIRING_REGISTRY 应有 4 条 wire；
    nodes_for_app('agent-service') 应包含 4 个节点。"""
    # 先清空再 import 确保不依赖测试顺序
    from app.runtime.wire import WIRING_REGISTRY, clear_wiring
    from app.runtime.placement import clear_bindings, nodes_for_app

    clear_wiring()
    clear_bindings()

    # import 触发 wire / bind
    import app.wiring.safety  # noqa: F401

    from app.domain.safety import (
        PostSafetyRequest,
        PreSafetyRequest,
        PreSafetyVerdict,
        Recall,
    )
    from app.nodes.safety import (
        resolve_pre_safety_waiter,
        run_post_safety,
        run_pre_safety,
    )
    from app.runtime.sink import SinkSpec

    # 4 条 wire
    by_type = {w.data_type: w for w in WIRING_REGISTRY}
    assert PreSafetyRequest in by_type
    assert PreSafetyVerdict in by_type
    assert PostSafetyRequest in by_type
    assert Recall in by_type

    # PreSafetyRequest -> run_pre_safety, in-process
    w_pre_req = by_type[PreSafetyRequest]
    assert run_pre_safety in w_pre_req.consumers
    assert w_pre_req.durable is False

    # PreSafetyVerdict -> resolve_pre_safety_waiter, in-process
    w_pre_v = by_type[PreSafetyVerdict]
    assert resolve_pre_safety_waiter in w_pre_v.consumers

    # PostSafetyRequest -> run_post_safety, durable
    w_post = by_type[PostSafetyRequest]
    assert run_post_safety in w_post.consumers
    assert w_post.durable is True

    # Recall -> Sink.mq("recall")
    w_recall = by_type[Recall]
    assert any(
        isinstance(s, SinkSpec) and s.kind == "mq" and s.params["queue"] == "recall"
        for s in w_recall.sinks
    )

    # placement
    own = nodes_for_app("agent-service")
    assert run_pre_safety in own
    assert resolve_pre_safety_waiter in own
    assert run_post_safety in own
```

- [ ] **Step 9.2: 跑测试确认 fail**

```
cd apps/agent-service && uv run pytest tests/unit/wiring/test_safety_wiring.py -v
```

Expected: ImportError on `app.wiring.safety`

- [ ] **Step 9.3: 写 `apps/agent-service/app/wiring/safety.py`**

```python
"""Phase 2 safety wiring.

Pre-check 控制面进 graph：chat pipeline emit(PreSafetyRequest) → run_pre_safety
→ PreSafetyVerdict → resolve_pre_safety_waiter（把 verdict 塞回本进程 Future）。

Post-check 数据面走 durable：chat pipeline emit(PostSafetyRequest) → durable
queue → run_post_safety → blocked 时 return Recall → Sink.mq("recall") →
lark-server recall-worker。

所有节点都跑在 agent-service 主进程；post 复用 agent-service 而不是新开
safety-worker，因为单条审计的工作量小（一次 banned word + 一次 guard LLM）。
"""
from app.domain.safety import (
    PostSafetyRequest,
    PreSafetyRequest,
    PreSafetyVerdict,
    Recall,
)
from app.nodes.safety import (
    resolve_pre_safety_waiter,
    run_post_safety,
    run_pre_safety,
)
from app.runtime import Sink, bind, wire

# Pre-check：双段 in-process wire
wire(PreSafetyRequest).to(run_pre_safety)
wire(PreSafetyVerdict).to(resolve_pre_safety_waiter)

# Post-check：durable
wire(PostSafetyRequest).to(run_post_safety).durable()

# Recall 出 graph 给 lark-server recall-worker
wire(Recall).to(Sink.mq("recall"))

# Placement — 4 个节点都在 agent-service 主进程
bind(run_pre_safety).to_app("agent-service")
bind(resolve_pre_safety_waiter).to_app("agent-service")
bind(run_post_safety).to_app("agent-service")
```

- [ ] **Step 9.4: 在 `apps/agent-service/app/wiring/__init__.py` 加 import**

定位现有内容（按字母序追加）：

```python
"""Import all wiring submodules so their ``wire(...)`` calls run on package import."""
import app.wiring.memory  # noqa: F401
import app.wiring.memory_vectorize  # noqa: F401
import app.wiring.safety  # noqa: F401  # Phase 2
```

- [ ] **Step 9.5: 跑测试确认 pass**

```
cd apps/agent-service && uv run pytest tests/unit/wiring/test_safety_wiring.py -v
```

Expected: 1 passed

- [ ] **Step 9.6: 跑全 wiring 测试 + compile_graph 启动 smoke**

```
cd apps/agent-service && uv run pytest tests/unit/wiring/ tests/unit/runtime/ -v
```

Expected: 全 pass。如果 `compile_graph()` 启动测试有 regression（之前 graph fixture 没 import safety wiring），按需更新 fixture。

- [ ] **Step 9.7: Commit**

```bash
git add apps/agent-service/app/wiring/safety.py apps/agent-service/app/wiring/__init__.py apps/agent-service/tests/unit/wiring/test_safety_wiring.py
git commit -m "feat(agent-service): wire safety pipeline (pre in-process, post durable, recall sink)"
```

---

## Task 10 — `main.py` lifespan：启 / 关 durable consumers

**Files:**
- Modify: `apps/agent-service/app/main.py`
- Test: 集成 smoke（lifespan 启动通过）

- [ ] **Step 10.1: 修改 `apps/agent-service/app/main.py` lifespan**

定位 line 56-66（旧 consumer 启动块）：

```python
    # Start MQ consumers (only when RabbitMQ is configured)
    consumer_tasks: list[asyncio.Task] = []
    if settings.rabbitmq_url:
        from app.workers.chat_consumer import start_chat_consumer
        from app.workers.post_consumer import start_post_consumer

        consumer_tasks.append(asyncio.create_task(start_post_consumer()))
        logger.info("Post safety consumer started")

        consumer_tasks.append(asyncio.create_task(start_chat_consumer()))
        logger.info("Chat request consumer started")
```

替换为：

```python
    # Start MQ consumers (only when RabbitMQ is configured)
    consumer_tasks: list[asyncio.Task] = []
    if settings.rabbitmq_url:
        from app.workers.chat_consumer import start_chat_consumer

        # Phase 2: post-safety 改走 runtime durable consumer。旧
        # start_post_consumer 删除（替代为 wire(PostSafetyRequest)
        # .to(run_post_safety).durable()）；runtime 自动按 placement.bind
        # 过滤启动属于本 app 的 consumer。
        from app.runtime.durable import start_consumers
        await start_consumers(app_name="agent-service")
        logger.info("Runtime durable consumers started for agent-service")

        consumer_tasks.append(asyncio.create_task(start_chat_consumer()))
        logger.info("Chat request consumer started")
```

定位 lifespan teardown（line 74-94）。在 `for task in consumer_tasks: task.cancel()` 之前加 runtime stop_consumers：

```python
    # Phase 2: stop runtime durable consumers cleanly before tearing
    # down RabbitMQ connection (otherwise late deliveries race with close).
    if settings.rabbitmq_url:
        from app.runtime.durable import stop_consumers
        await stop_consumers()

    # Shutdown legacy consumers (chat consumer)
    for task in consumer_tasks:
        task.cancel()
        ...
```

完整修改后片段：

```python
    yield

    # Shutdown
    if settings.rabbitmq_url:
        from app.runtime.durable import stop_consumers
        await stop_consumers()

    for task in consumer_tasks:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception) as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.warning("Consumer task ended with error: %s", e)

    # Cancel skill reload task
    reload_task.cancel()
    try:
        await reload_task
    except asyncio.CancelledError:
        pass

    # Close RabbitMQ connection
    if settings.rabbitmq_url:
        from app.infra.rabbitmq import mq
        await mq.close()
```

- [ ] **Step 10.2: 跑现有 main / lifespan 测试**

```
cd apps/agent-service && uv run pytest tests/ -v -k "main or lifespan or startup" --no-header
```

Expected: 不 regression。如果项目里没专门 lifespan 测试，做 import smoke：

```
cd apps/agent-service && uv run python -c "from app.main import app; print('lifespan OK', app.title)"
```

Expected: 输出 "lifespan OK FastAPI"（或类似）。

- [ ] **Step 10.3: Commit**

```bash
git add apps/agent-service/app/main.py
git commit -m "feat(agent-service): boot runtime durable consumers in lifespan, drop start_post_consumer"
```

---

## Task 11 — `chat/post_actions.py`：改成 emit `PostSafetyRequest`

**Files:**
- Modify: `apps/agent-service/app/chat/post_actions.py`
- Test: `apps/agent-service/tests/unit/chat/test_post_actions.py` (修改现有测试)

- [ ] **Step 11.1: 找到现有 post_actions 测试并改写**

`apps/agent-service/tests/unit/chat/test_post_actions.py`（如果不存在则新建）：

```python
"""Tests for chat/post_actions.py — Phase 2 emit(PostSafetyRequest)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.domain.safety import PostSafetyRequest


@pytest.mark.asyncio
async def test_publish_post_check_emits_post_safety_request():
    """旧 mq.publish(SAFETY_CHECK,...) 替换为 emit(PostSafetyRequest(...))。"""
    from app.chat.post_actions import _publish_post_check

    captured: list[PostSafetyRequest] = []

    async def fake_emit(data):
        captured.append(data)

    with patch("app.chat.post_actions.emit", fake_emit):
        await _publish_post_check(
            session_id="sess-1",
            response_text="hello",
            chat_id="chat-1",
            trigger_message_id="msg-1",
        )

    assert len(captured) == 1
    req = captured[0]
    assert isinstance(req, PostSafetyRequest)
    assert req.session_id == "sess-1"
    assert req.trigger_message_id == "msg-1"
    assert req.chat_id == "chat-1"
    assert req.response_text == "hello"


@pytest.mark.asyncio
async def test_publish_post_check_swallows_emit_errors():
    """emit 抛异常不应炸 chat pipeline（fire-and-forget 语义保留）。"""
    from app.chat.post_actions import _publish_post_check

    async def fake_emit(data):
        raise RuntimeError("mq down")

    with patch("app.chat.post_actions.emit", fake_emit):
        # 不应抛
        await _publish_post_check(
            session_id="sess-1",
            response_text="hello",
            chat_id="chat-1",
            trigger_message_id="msg-1",
        )
```

- [ ] **Step 11.2: 跑测试确认 fail**

```
cd apps/agent-service && uv run pytest tests/unit/chat/test_post_actions.py -v
```

Expected: 旧 `_publish_post_check` 还在用 `mq.publish(SAFETY_CHECK,...)`，测试 mock 路径不对会 fail。

- [ ] **Step 11.3: 修改 `apps/agent-service/app/chat/post_actions.py`**

定位 line 14-15 imports 和 line 32-52 `_publish_post_check`：

旧：

```python
from app.api.middleware import get_lane
from app.infra.rabbitmq import SAFETY_CHECK, mq
from app.memory._persona import load_persona
```

```python
async def _publish_post_check(
    session_id: str,
    response_text: str,
    chat_id: str,
    trigger_message_id: str,
) -> None:
    """Publish post safety check payload to RabbitMQ."""
    try:
        await mq.publish(
            SAFETY_CHECK,
            {
                "session_id": session_id,
                "response_text": response_text,
                "chat_id": chat_id,
                "trigger_message_id": trigger_message_id,
                "lane": get_lane(),
            },
        )
        logger.info("Published post safety check: session_id=%s", session_id)
    except Exception as e:
        logger.error("Failed to publish post safety check: %s", e)
```

替换为：

```python
from app.domain.safety import PostSafetyRequest
from app.memory._persona import load_persona
from app.runtime.emit import emit
```

```python
async def _publish_post_check(
    session_id: str,
    response_text: str,
    chat_id: str,
    trigger_message_id: str,
) -> None:
    """Emit PostSafetyRequest into the dataflow graph (Phase 2).

    Replaces ``mq.publish(SAFETY_CHECK, ...)``: the wire
    ``wire(PostSafetyRequest).to(run_post_safety).durable()`` in
    ``app/wiring/safety.py`` queues the request and the durable consumer
    bound on agent-service runs the audit.
    """
    try:
        await emit(PostSafetyRequest(
            session_id=session_id,
            trigger_message_id=trigger_message_id,
            chat_id=chat_id,
            response_text=response_text,
        ))
        logger.info("Emitted PostSafetyRequest: session_id=%s", session_id)
    except Exception as e:
        logger.error("Failed to emit PostSafetyRequest: %s", e)
```

注意：`get_lane` import 如果其他函数还在用就保留，否则一起删；本次只为 `_publish_post_check` 服务的 `from app.api.middleware import get_lane` 删除。

- [ ] **Step 11.4: 跑测试确认 pass**

```
cd apps/agent-service && uv run pytest tests/unit/chat/test_post_actions.py -v
```

Expected: 2 passed

- [ ] **Step 11.5: Commit**

```bash
git add apps/agent-service/app/chat/post_actions.py apps/agent-service/tests/unit/chat/test_post_actions.py
git commit -m "feat(agent-service): replace mq.publish(SAFETY_CHECK) with emit(PostSafetyRequest)"
```

---

## Task 12 — `chat/pipeline.py`：改 `run_pre_safety_via_graph` + `_buffer_until_pre` 字段映射

**Files:**
- Modify: `apps/agent-service/app/chat/pipeline.py`
- Test: `apps/agent-service/tests/unit/chat/test_pipeline.py` (修改现有 pre 路径测试)

- [ ] **Step 12.1: 检查现有 pipeline 测试**

```bash
cd apps/agent-service && grep -n "run_pre_check\|PreCheckResult\|_buffer_until_pre" tests/unit/chat/test_pipeline.py
```

把所有 `run_pre_check` 引用换成 `pre_safety_gate.run_pre_safety_via_graph`，`PreCheckResult` 换成 `PreSafetyVerdict`。验收主要语义：

- pre 阻断 → yield guard_message
- pre 通过 → yield 正常 stream

- [ ] **Step 12.2: 修改 `apps/agent-service/app/chat/pipeline.py` line 99-102**

定位旧代码（line 99-102）：

```python
            # 3. Pre-safety check (parallel with streaming)
            pre_task = asyncio.create_task(
                run_pre_check(parsed.render(), persona_id=effective_persona)
            )
```

替换为：

```python
            # 3. Pre-safety check (parallel with streaming) — Phase 2:
            # 走 graph：emit(PreSafetyRequest) → run_pre_safety → 装饰器 emit
            # PreSafetyVerdict → resolve_pre_safety_waiter 把 verdict 塞回
            # 本进程 Future。run_pre_safety_via_graph 内部 fail-open 集中
            # 处理 timeout / emit 异常 / 外层 cancel。
            pre_task = asyncio.create_task(
                pre_safety_gate.run_pre_safety_via_graph(
                    message_id=message_id,
                    content=parsed.render(),
                    persona_id=effective_persona,
                )
            )
```

定位 line 34（旧 import `from app.chat.safety import run_pre_check`），改成：

```python
from app.chat import pre_safety_gate
```

如果有别处也 import `run_pre_check` / `PreCheckResult`，全部清掉。

- [ ] **Step 12.3: 修改 `_buffer_until_pre` 读 verdict 字段**

定位 `_buffer_until_pre` 函数体（line 316 附近）。原代码读 `result.is_blocked` / `result.block_reason` / `result.detail`（来自 `PreCheckResult` dataclass）。新对象 `PreSafetyVerdict` 字段名一致（is_blocked / block_reason / detail），**直接复用，不需要改**。

仔细确认一下：原代码具体在哪里用 result：

```bash
cd apps/agent-service && grep -n -A5 "is_blocked\|block_reason" app/chat/pipeline.py | head -40
```

如果命中点都是 `verdict.is_blocked` / `verdict.block_reason`，命名一致；否则按需改名。

- [ ] **Step 12.4: 跑现有 pipeline 测试 + 新加的 pre 路径测试**

```
cd apps/agent-service && uv run pytest tests/unit/chat/test_pipeline.py -v
```

Expected: 全 pass。如果有 fail 是因为旧 mock `run_pre_check` 没改，按 Step 12.1 把 mock 路径换成 `pre_safety_gate.run_pre_safety_via_graph`。

- [ ] **Step 12.5: Commit**

```bash
git add apps/agent-service/app/chat/pipeline.py apps/agent-service/tests/unit/chat/test_pipeline.py
git commit -m "feat(agent-service): pipeline pre-check goes through dataflow graph"
```

---

## Task 13 — 删除旧文件 `chat/safety.py` + `workers/post_consumer.py`

**Files:**
- Delete: `apps/agent-service/app/chat/safety.py`
- Delete: `apps/agent-service/app/workers/post_consumer.py`
- Delete: `apps/agent-service/tests/unit/chat/test_safety.py`（如有）

- [ ] **Step 13.1: 确认无残留 import**

```bash
cd apps/agent-service && grep -rn "from app.chat.safety\|from app.workers.post_consumer\|app\.chat\.safety\|app\.workers\.post_consumer" app/ tests/ 2>/dev/null
```

Expected: 零结果（前置 task 已经把所有 import 切走）。

- [ ] **Step 13.2: 删除文件**

```bash
git rm apps/agent-service/app/chat/safety.py
git rm apps/agent-service/app/workers/post_consumer.py
# 老测试文件如果还在
git rm apps/agent-service/tests/unit/chat/test_safety.py 2>/dev/null || true
```

- [ ] **Step 13.3: 跑全测试做 regression**

```
cd apps/agent-service && uv run pytest tests/ -v
```

Expected: 全 pass。如果有 fail：
- 测试还 import 旧模块 → 改 import 路径
- workers 还有 import post_consumer → grep 一下定位

- [ ] **Step 13.4: Commit**

```bash
git commit -m "chore(agent-service): remove chat/safety.py and workers/post_consumer.py (replaced by dataflow nodes)"
```

---

## Task 14 — lark-server `recall-worker.ts` max retry 写 `recall_failed`

**Files:**
- Modify: `apps/lark-server/src/workers/recall-worker.ts`

- [ ] **Step 14.1: 定位修改点**

`apps/lark-server/src/workers/recall-worker.ts:73-78`：

```typescript
        // 达到最大重试次数，nack → DLQ
        console.error(
            `[RecallWorker] Max retries reached for session_id=${session_id}, sending to DLQ`,
        );
        rabbitmqClient.nack(msg, false);
        return;
```

- [ ] **Step 14.2: 改成在 nack 之前写 `recall_failed`**

替换为：

```typescript
        // 达到最大重试次数：在进 DLQ 之前写 recall_failed 终态，
        // 避免新链路下 status 永远停在 pending（Phase 2 §4.4）
        console.error(
            `[RecallWorker] Max retries reached for session_id=${session_id}, marking recall_failed and sending to DLQ`,
        );
        try {
            await repo.update(
                { session_id },
                {
                    safety_status: 'recall_failed',
                    safety_result: {
                        reason,
                        detail,
                        recalled: 0,
                        failed: 0,
                        checked_at: new Date().toISOString(),
                    },
                },
            );
        } catch (e) {
            console.error(`[RecallWorker] Failed to write recall_failed status:`, e);
        }
        rabbitmqClient.nack(msg, false);
        return;
```

- [ ] **Step 14.3: 跑 lark-server 测试**

```bash
cd apps/lark-server && bun test src/workers/
```

Expected: 全 pass。如果项目对 recall-worker 没单元测试（只在 dev 泳道集成测试），跳过这步。

- [ ] **Step 14.4: Lint / type check**

```bash
cd apps/lark-server && bun run typecheck
```

Expected: 无报错。

- [ ] **Step 14.5: Commit**

```bash
git add apps/lark-server/src/workers/recall-worker.ts
git commit -m "fix(recall-worker): write recall_failed before DLQ on max retry"
```

---

## Task 15 — 启动 sanity（compile_graph + start_consumers smoke）

**Files:**
- 不改代码；只跑启动 smoke 验证整个改造能 boot

- [ ] **Step 15.1: import-only smoke**

```bash
cd apps/agent-service && uv run python -c "
from app.runtime.graph import compile_graph
import app.wiring  # registers all wires
g = compile_graph()
print(f'compile_graph OK: {len(g.wires)} wires, {sum(len(w.consumers) for w in g.wires)} consumers, {sum(len(w.sinks) for w in g.wires)} sinks')

# 检查 4 条 safety wire 都注册了
from app.domain.safety import PreSafetyRequest, PreSafetyVerdict, PostSafetyRequest, Recall
by_type = {w.data_type for w in g.wires}
assert PreSafetyRequest in by_type
assert PreSafetyVerdict in by_type
assert PostSafetyRequest in by_type
assert Recall in by_type
print('safety wires registered: OK')

# Recall 必须有 Sink.mq('recall')
recall_w = next(w for w in g.wires if w.data_type is Recall)
assert any(s.kind == 'mq' and s.params['queue'] == 'recall' for s in recall_w.sinks), recall_w.sinks
print('Recall -> Sink.mq(recall): OK')
"
```

Expected: 输出 OK 三行，无 exception。

- [ ] **Step 15.2: 跑全 unit 测试做最终 regression**

```bash
cd apps/agent-service && uv run pytest tests/ -v
```

Expected: 全 pass。

- [ ] **Step 15.3: 跑 lint / type check（按项目惯例）**

```bash
cd apps/agent-service && uv run ruff check app/ tests/
cd apps/agent-service && uv run mypy app/  # 如有
```

Expected: 无 error。

- [ ] **Step 15.4: 最后一个 commit（空 commit 标记 milestone）**

```bash
git commit --allow-empty -m "chore: phase 2 safety dataflow ready for lane verification"
```

---

## 后续：泳道部署 + 验收

按 spec §6 走，**两个镜像同步部署**：

1. **泳道 deploy**

```bash
make deploy APP=lark-server LANE=phase2-safety GIT_REF=$(git rev-parse HEAD)
make deploy APP=agent-service LANE=phase2-safety GIT_REF=$(git rev-parse HEAD)
```

注意一镜像多服务铁律：
- agent-service 镜像 → `agent-service` / `arq-worker` / `vectorize-worker` 三个 Deployment 同步 release
- lark-server 镜像 → `lark-server` / `recall-worker` / `chat-response-worker` 三个 Deployment 同步 release

2. **绑 dev bot**

```bash
/ops bind TYPE=bot KEY=dev LANE=phase2-safety
```

3. **跑 4 类消息验证**

| 场景 | 预期 |
|---|---|
| pre block | dev bot 收到 guard_message，没正常回复 |
| pre pass | dev bot 正常回复 |
| post block | dev bot 回复后被 recall-worker 撤回；`agent_responses.safety_status='recalled'` |
| post pass | dev bot 回复保留；`agent_responses.safety_status='passed'` |

4. **观测**

```bash
make logs APP=agent-service LANE=phase2-safety KEYWORD="durable consumer started"
make logs APP=agent-service LANE=phase2-safety KEYWORD="post safety"
make logs APP=recall-worker LANE=phase2-safety KEYWORD="Recall"
```

DB 校验：

```sql
-- 通过 /ops-db @chiwei
SELECT session_id, safety_status, safety_result
FROM agent_responses
WHERE session_id IN (...);
```

5. **prod ship 顺序（先 lark-server 后 agent-service，spec §6）**

```bash
# 先 lark-server（让 max retry fix 先生效，避免 agent-service 切新链路后命中老 hole）
make deploy APP=lark-server LANE=prod
# 5 min 观察后 agent-service
make deploy APP=agent-service LANE=prod
```

6. **24h 稳定后 followup**

- `SAFETY_CHECK` route 从 `ALL_ROUTES` 移除 + 删 `safety_check` 队列（独立 PR）
- lark-server `trigger_message_id` 幂等（独立 PR）

---

## 验收 checklist（spec §8 拷贝）

- [ ] `grep -rn "SAFETY_CHECK\|safety_check" apps/agent-service/app/chat apps/agent-service/app/workers` 零结果
- [ ] `grep -rn "mq.publish" apps/agent-service/app/chat apps/agent-service/app/nodes/safety.py` 零结果
- [ ] `apps/agent-service/app/workers/post_consumer.py` 不存在
- [ ] `apps/agent-service/app/chat/safety.py` 不存在
- [ ] `compile_graph()` 接受 `wire(Recall).to(Sink.mq("recall"))`，并对 `Sink.mq("not_in_routes")` 启动报错
- [ ] 泳道部署后 `make logs APP=agent-service KEYWORD=consumer` 出现 `durable consumer started: durable_post_safety_request_run_post_safety_<lane> -> run_post_safety`
- [ ] 4 种 case（pre block / pre pass / post block / post pass）泳道验证全过
- [ ] `agent_responses.safety_status` 在新链路下：passed 路径 `pending → passed`；blocked 路径 `pending → recalled / recall_failed`（不经过 "blocked" 中间状态）
- [ ] `grep -n "await emit" apps/agent-service/app/nodes/safety.py` 零结果（无手动 emit，全靠 @node 装饰器）
- [ ] lark-server recall-worker 收到 Recall 消息（payload schema 与改造前一致，`payload.lane` 字段填充正确）
- [ ] DLQ replay runbook 补充 "查 safety_status 再决定 replay" 提示
- [ ] `apps/lark-server/src/workers/recall-worker.ts` max retry 分支补 `safety_status="recall_failed"` 写入
- [ ] 单元测试覆盖：`run_post_safety` 在 row missing 时 raise；`run_pre_safety_via_graph` 在节点 21s 卡住时超时 fail-open + emit_task 被 cancel；外层 cancel 时 emit_task 被 cancel

---

**Plan ends here. 实现按 Task 1 → Task 15 顺序执行；Phase A/B 完成后即可独立测试 Phase C/D；Phase E 在 Phase C/D 完成后才能切换。**
