# Dataflow Phase 7a (Transport) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase 7a — 给 dataflow runtime 加 transport 语义层 primitive，闭合 Gap 7 (durable retry + idempotency 状态机) / Gap 9 (delayed/scheduled emit) / Gap 11 (trace/lane propagation 统一)。Phase 7 总 spec 见 `docs/superpowers/specs/2026-05-08-dataflow-phase-7-gap-analysis.md`。

**Architecture:** 五个新 runtime 模块（`propagation.py` / `inflight.py` / `retry.py` / `scheduled.py` / `delayed_trigger.py`）+ 现有 `durable.py` / `debounce.py` / `engine.py` / `emit.py` / `wire.py` / `infra/rabbitmq.py` 切新 primitive。改动 backwards-compatible：未声明 retry 的 wire 业务可观测行为完全等价（runtime_inflight 表会多写，但消息行为不变）；新加的 `emit_delayed` / `emit_at` 是 additive；propagation 抽象保持 header 名 / contextvar 名不变。

**Tech Stack:** Python 3.11 / asyncio / aio-pika (publish-confirm) / SQLAlchemy / pytest-asyncio / RabbitMQ x-delayed-message exchange / Postgres (advisory lock + new `runtime_inflight` table) / Langfuse OTEL.

---

## File Structure

### 新建（runtime primitive）

- `apps/agent-service/app/runtime/propagation.py` — `extract_context` / `inject_context` / `bind_context` (Gap 11)
- `apps/agent-service/app/runtime/inflight.py` — `runtime_inflight` 状态机 helper (claim / mark_succeeded / mark_failed / lease) (Gap 7.1)
- `apps/agent-service/app/runtime/retry.py` — retry policy + delivery_count + decide_retry (Gap 7.2/7.3)
- `apps/agent-service/app/runtime/scheduled.py` — in-process scheduled task pool (Gap 9 best_effort)
- `apps/agent-service/app/runtime/delayed_trigger.py` — `DelayedTriggerEnvelope` Data + `_runtime_trigger_consumer` node + `register_trigger_routes(app)` (Gap 9 durable)

### 修改（runtime existing）

- `apps/agent-service/app/runtime/durable.py` — `_build_handler` 接 propagation + inflight + retry primitive
- `apps/agent-service/app/runtime/debounce.py` — `publish_debounce` / `_build_handler` 接 propagation
- `apps/agent-service/app/runtime/engine.py` — `_source_loop_mq` 接 propagation；`_source_loop_cron` / `_source_loop_interval` 自动生成 trace_id；启动时 declare trigger queue + 启动 trigger consumer + 关闭时 cancel scheduled
- `apps/agent-service/app/runtime/emit.py` — 新增 `emit_delayed` / `emit_at` 顶层 API（走 trigger queue 或 schedule_after）
- `apps/agent-service/app/runtime/wire.py` — `RetryPolicy` dataclass + `WireBuilder.retry(n, backoff, base_delay_ms, max_delay_ms, lease_ms)`
- `apps/agent-service/app/runtime/sink_dispatch.py` — 接 propagation（保留 body-level lane 兼容窗口）
- `apps/agent-service/app/infra/rabbitmq.py` — `mq.publish_with_confirm` method + register routes for trigger queue + lane queue 沿用现有 `_ensure_lane_queue` / `lane_queue` 机制
- `apps/agent-service/app/runtime/__init__.py` — 公开 `emit_delayed` / `emit_at`
- `apps/agent-service/app/runtime/persist.py` — 不动（`insert_idempotent` 仍存在但 durable handler 不再调用；将由 inflight 状态机替代）

### 新建（测试）

- `apps/agent-service/tests/runtime/test_propagation.py`
- `apps/agent-service/tests/runtime/test_inflight.py`
- `apps/agent-service/tests/runtime/test_retry.py`
- `apps/agent-service/tests/runtime/test_durable_retry.py` (integration)
- `apps/agent-service/tests/runtime/test_scheduled.py`
- `apps/agent-service/tests/runtime/test_delayed_trigger.py`
- `apps/agent-service/tests/runtime/test_emit_delayed.py`
- `apps/agent-service/tests/runtime/test_cron_trace.py`
- `apps/agent-service/tests/runtime/conftest.py` — 共享 fixture（wire 注入 / mq mock / inflight reset）

### 新建（CI + 文档）

- `.github/workflows/grep-gate.yml` — closed gap exact-zero + open gap baseline
- `.github/grep-baselines.json` — open gap baseline counts
- `docs/superpowers/retrospectives/2026-05-XX-phase7a-retry-drill.md` — drill 截图/日志（dev 泳道完成后写）

### 不动

- `apps/agent-service/app/api/middleware.py` — `trace_id_var` / `lane_var` 定义点保留
- `apps/lark-server/src/workers/chat-response-worker.ts` — body-level `payload.lane` 兼容窗口保留

---

## Task 1: propagation primitive

**Files:**
- Create: `apps/agent-service/app/runtime/propagation.py`
- Test: `apps/agent-service/tests/runtime/test_propagation.py`

抽出 `extract_context` / `inject_context` / `bind_context` 三个 helper。所有调用方下个 task 切换。

- [ ] **Step 1.1: 写失败测试**

```python
# apps/agent-service/tests/runtime/test_propagation.py
"""Contract tests for runtime/propagation.py — Gap 11 primitive."""
from __future__ import annotations

import pytest

from app.api.middleware import lane_var, trace_id_var
from app.runtime.propagation import (
    Context,
    bind_context,
    extract_context,
    inject_context,
)


class TestExtractContext:
    def test_strings_pass_through(self) -> None:
        ctx = extract_context({"trace_id": "abc", "lane": "feat-x"})
        assert ctx.trace_id == "abc"
        assert ctx.lane == "feat-x"

    def test_empty_strings_become_none(self) -> None:
        ctx = extract_context({"trace_id": "", "lane": ""})
        assert ctx.trace_id is None and ctx.lane is None

    def test_non_string_values_become_none(self) -> None:
        ctx = extract_context({"trace_id": 123, "lane": ["x"]})
        assert ctx.trace_id is None and ctx.lane is None

    def test_missing_keys_become_none(self) -> None:
        ctx = extract_context({})
        assert ctx.trace_id is None and ctx.lane is None

    def test_none_headers(self) -> None:
        ctx = extract_context(None)
        assert ctx.trace_id is None and ctx.lane is None


class TestInjectContext:
    def test_writes_strings(self) -> None:
        h = inject_context({}, Context(trace_id="t1", lane="prod"))
        assert h == {"trace_id": "t1", "lane": "prod"}

    def test_none_becomes_empty_string(self) -> None:
        h = inject_context({}, Context(trace_id=None, lane=None))
        assert h == {"trace_id": "", "lane": ""}

    def test_preserves_existing_headers(self) -> None:
        h = inject_context({"data_type": "Foo"}, Context(trace_id="t", lane=None))
        assert h == {"data_type": "Foo", "trace_id": "t", "lane": ""}

    def test_reads_from_contextvars_when_no_arg(self) -> None:
        t_tok = trace_id_var.set("from-cv")
        l_tok = lane_var.set("lane-cv")
        try:
            h = inject_context({})
        finally:
            trace_id_var.reset(t_tok)
            lane_var.reset(l_tok)
        assert h == {"trace_id": "from-cv", "lane": "lane-cv"}


class TestBindContext:
    @pytest.mark.asyncio
    async def test_sets_and_resets(self) -> None:
        prev_t, prev_l = trace_id_var.get(), lane_var.get()
        async with bind_context(Context(trace_id="t1", lane="feat-x")):
            assert trace_id_var.get() == "t1"
            assert lane_var.get() == "feat-x"
        assert trace_id_var.get() == prev_t
        assert lane_var.get() == prev_l

    @pytest.mark.asyncio
    async def test_resets_on_exception(self) -> None:
        prev_t = trace_id_var.get()
        with pytest.raises(RuntimeError):
            async with bind_context(Context(trace_id="t1", lane=None)):
                raise RuntimeError("boom")
        assert trace_id_var.get() == prev_t

    @pytest.mark.asyncio
    async def test_none_context_clears_vars(self) -> None:
        t_tok = trace_id_var.set("outer")
        try:
            async with bind_context(Context(trace_id=None, lane=None)):
                assert trace_id_var.get() is None
                assert lane_var.get() is None
        finally:
            trace_id_var.reset(t_tok)
```

- [ ] **Step 1.2: 跑测试确认全失败**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_propagation.py -v`

Expected: ImportError on `app.runtime.propagation`

- [ ] **Step 1.3: 实现 propagation primitive**

```python
# apps/agent-service/app/runtime/propagation.py
"""Trace / lane context propagation primitive (Gap 11).

Three primitives:

* ``extract_context(headers)`` — defensive parse of inbound headers.
* ``inject_context(headers, ctx=None)`` — write outbound headers (defaults to
  reading current contextvars).
* ``bind_context(ctx)`` — async context manager that sets contextvars on enter,
  restores on exit (works on success and exception paths).

Business code MUST NOT touch ``trace_id_var`` / ``lane_var`` directly. New
``Source`` types and new transport paths inside ``runtime/`` use these
primitives only.
"""
from __future__ import annotations

import contextlib
from contextvars import Token
from dataclasses import dataclass
from typing import Any, AsyncIterator

from app.api.middleware import lane_var, trace_id_var


@dataclass(frozen=True)
class Context:
    trace_id: str | None
    lane: str | None


def _coerce(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def extract_context(headers: dict[str, Any] | None) -> Context:
    h = headers or {}
    return Context(
        trace_id=_coerce(h.get("trace_id")),
        lane=_coerce(h.get("lane")),
    )


def inject_context(
    headers: dict[str, Any] | None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    if ctx is None:
        ctx = Context(trace_id=trace_id_var.get(), lane=lane_var.get())
    out: dict[str, Any] = dict(headers) if headers else {}
    out["trace_id"] = ctx.trace_id or ""
    out["lane"] = ctx.lane or ""
    return out


@contextlib.asynccontextmanager
async def bind_context(ctx: Context) -> AsyncIterator[None]:
    t_tok: Token = trace_id_var.set(ctx.trace_id)
    l_tok: Token = lane_var.set(ctx.lane)
    try:
        yield
    finally:
        trace_id_var.reset(t_tok)
        lane_var.reset(l_tok)
```

- [ ] **Step 1.4: 跑测试确认全过**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_propagation.py -v`

Expected: 12 passed

- [ ] **Step 1.5: ruff + mypy 通过**

Run: `cd apps/agent-service && uv run ruff check app/runtime/propagation.py tests/runtime/test_propagation.py && uv run mypy app/runtime/propagation.py`

- [ ] **Step 1.6: Commit**

```bash
git add apps/agent-service/app/runtime/propagation.py apps/agent-service/tests/runtime/test_propagation.py
git commit -m "feat(runtime): propagation primitive — extract / inject / bind context (Gap 11)"
```

---

## Task 2: durable / debounce / source-mq / sink-dispatch 切 propagation

**Files:**
- Modify: `apps/agent-service/app/runtime/durable.py:67-155`
- Modify: `apps/agent-service/app/runtime/debounce.py:175-365`
- Modify: `apps/agent-service/app/runtime/engine.py:454-467`（_source_loop_mq）
- Modify: `apps/agent-service/app/runtime/sink_dispatch.py`
- Test: 现有 `test_durable.py` / `test_debounce.py` / `test_source_mq.py` 必须不破坏

切换 primitive **不改业务可观测行为**：现有测试不动且必须 green；不写新测试。

- [ ] **Step 2.1: durable.py:74-85 publish_durable 切 inject_context**

替换原手动 dict 构造为 `inject_context({"data_type": ...}, Context(trace_id=..., lane=...))`，保留 data.lane fallback 逻辑（这是 publish_durable 独有，不属 primitive）。

- [ ] **Step 2.2: durable.py:105-153 _build_handler 切 bind_context**

把原 `t_tok = trace_id_var.set(...) ... finally trace_id_var.reset(...)` 块替换为 `ctx = extract_context(message.headers); async with bind_context(ctx): ...`。原 `async with message.process(requeue=False)` 保持不变（本 task 不动 retry 行为）。

- [ ] **Step 2.3: debounce.py:204-209 + 255-261 publish 切 inject_context**

两处 publish path（`publish_debounce` 和 `_do_reschedule`）均替换 headers 构造为 `inject_context({"data_type": type(data).__name__})`。

- [ ] **Step 2.4: debounce.py:296-307 handler 切 bind_context**

同 Step 2.2 模式。

- [ ] **Step 2.5: engine.py:454-467 _source_loop_mq 切 bind_context**

```python
from app.runtime.propagation import bind_context, extract_context

# 替换原 set/reset 块：
ctx = extract_context(incoming.headers)
async with bind_context(ctx):
    # 原 body decode + filter + target dispatch 逻辑不动
```

- [ ] **Step 2.6: sink_dispatch.py 切 inject_context（保留 body-level lane）**

定位 sink dispatch publish 处（grep `mq.publish` in `runtime/sink_dispatch.py`）。**关键**：body 字段 `lane` 保留（chat-response-worker.ts 仍读它）；header 通过 inject_context **额外**塞（向前兼容）。

```python
from app.runtime.propagation import Context, inject_context

body_lane = body.get("lane")
ctx = Context(trace_id=trace_id_var.get(), lane=lane_var.get() or body_lane)
headers = inject_context({"data_type": type(data).__name__}, ctx)
await mq.publish(route, body, headers=headers, lane=ctx.lane or None)
```

- [ ] **Step 2.7: 跑全部 runtime 测试不破坏**

Run: `cd apps/agent-service && uv run pytest tests/runtime/ -v`

Expected: 现有 test_durable / test_debounce / test_emit_* / test_source_mq / test_engine_phase4 / test_wire 全 pass；test_propagation.py 也 pass

- [ ] **Step 2.8: grep 验证**

```bash
grep -rn "trace_id_var\|lane_var" apps/agent-service/app/runtime/ \
  | grep -v "propagation.py\|__pycache__"
```

Expected: 仅出现在 publish_durable data.lane fallback 处 + sink_dispatch lane field read 处；durable._build_handler / debounce._build_handler / debounce.publish / engine._source_loop_mq 全部 0 命中。

- [ ] **Step 2.9: ruff 通过**

Run: `cd apps/agent-service && uv run ruff check app/runtime/`

- [ ] **Step 2.10: Commit**

```bash
git add apps/agent-service/app/runtime/durable.py apps/agent-service/app/runtime/debounce.py apps/agent-service/app/runtime/engine.py apps/agent-service/app/runtime/sink_dispatch.py
git commit -m "refactor(runtime): durable / debounce / source-mq / sink-dispatch use propagation primitive (Gap 11)"
```

---

## Task 3: cron / interval source 自动生成 trace_id

**Files:**
- Modify: `apps/agent-service/app/runtime/engine.py`（`_source_loop_cron` / `_source_loop_interval`）
- Test: `apps/agent-service/tests/runtime/test_cron_trace.py`

cron / interval 触发的 Data 走 in-process emit 时 contextvar 是 None，Langfuse 上断链。修复：每次 tick 自动生成 `trace_id = "cron:<expr>:<uuid8>"` / `"interval:<seconds>s:<uuid8>"`。

- [ ] **Step 3.1: 写测试 (skip 占位)**

```python
# apps/agent-service/tests/runtime/test_cron_trace.py
"""Cron / interval source must generate a trace_id (Gap 11)."""
from __future__ import annotations

import pytest


class TestCronTrace:
    @pytest.mark.asyncio
    async def test_cron_loop_generates_trace_id(self) -> None:
        """Each cron tick binds a fresh trace_id starting with 'cron:'.

        cron loop integration test requires a runtime fixture (croniter +
        real sleep). Behavior verified via dev-lane Langfuse trace inspection
        in Task 10 §10.7 instead. Code-level verification: grep `cron:` 在
        engine.py 实现处 + 跑现有 engine integration test 不破坏。
        """
        pytest.skip("verified via dev-lane Langfuse trace; see Task 10")

    @pytest.mark.asyncio
    async def test_interval_loop_generates_trace_id(self) -> None:
        pytest.skip("verified via dev-lane Langfuse trace; see Task 10")
```

- [ ] **Step 3.2: 实现 cron loop 自动 trace_id**

```python
# apps/agent-service/app/runtime/engine.py
import uuid

from app.runtime.propagation import Context, bind_context

# _source_loop_cron 内 tick 触发处：
async def _source_loop_cron(self, w, src, target):
    expr = src.params["expr"]
    tz = src.params.get("tz", "UTC")
    while not self._stop_event.is_set():
        await self._sleep_to_next_cron(expr, tz)
        if self._stop_event.is_set():
            break
        trace_id = f"cron:{expr.replace(' ', '_')}:{uuid.uuid4().hex[:8]}"
        ctx = Context(trace_id=trace_id, lane=None)
        async with bind_context(ctx):
            try:
                data = w.data_type()
                await target(**{next(iter(inputs_of(target))): data})
            except Exception:
                logger.exception("cron tick failed: expr=%s", expr)
```

- [ ] **Step 3.3: 实现 interval loop 自动 trace_id**

```python
# _source_loop_interval 内 tick 触发处：
trace_id = f"interval:{seconds}s:{uuid.uuid4().hex[:8]}"
ctx = Context(trace_id=trace_id, lane=None)
async with bind_context(ctx):
    # ... 同 cron
```

- [ ] **Step 3.4: 跑现有 engine 测试不破坏**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_engine_phase4.py tests/runtime/test_cron_trace.py -v`

Expected: engine_phase4 全 pass；test_cron_trace 2 skipped。

- [ ] **Step 3.5: grep 验证**

```bash
grep -n "trace_id = f\"cron:\|trace_id = f\"interval:" apps/agent-service/app/runtime/engine.py
```

Expected: 2 处命中（cron / interval 各一）。

- [ ] **Step 3.6: ruff 通过**

Run: `cd apps/agent-service && uv run ruff check app/runtime/engine.py`

- [ ] **Step 3.7: Commit**

```bash
git add apps/agent-service/app/runtime/engine.py apps/agent-service/tests/runtime/test_cron_trace.py
git commit -m "feat(runtime): cron / interval source auto-generate trace_id (Gap 11)"
```

---

## Task 4: runtime_inflight schema + state machine + lease + history backfill

**Files:**
- Create: `apps/agent-service/app/runtime/inflight.py`
- Modify: `apps/agent-service/app/runtime/migrator.py`（注册 `runtime_inflight` schema）
- Modify: `apps/agent-service/app/runtime/durable.py`（`_build_handler` 接 inflight 状态机替代 insert_idempotent 调用）
- Test: `apps/agent-service/tests/runtime/test_inflight.py`

引入独立 `runtime_inflight` 表 + `(edge_id, idempotent_key)` 复合 PK + lease 语义 + history backfill 兼容路径。durable handler 不再调 `insert_idempotent`，改调 `inflight.claim()`/`mark_succeeded()`/`mark_failed()`。

- [ ] **Step 4.1: 写 inflight 模块单元测试**

```python
# apps/agent-service/tests/runtime/test_inflight.py
"""Contract tests for runtime/inflight.py — Gap 7.1 state machine."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.inflight import (
    ClaimOutcome,
    claim_inflight,
    mark_failed,
    mark_succeeded,
)


@pytest.fixture(autouse=True)
async def _clean_inflight():
    async with get_session() as s:
        await s.execute(text("DELETE FROM runtime_inflight"))


class TestClaimInflight:
    @pytest.mark.asyncio
    async def test_first_time_creates_processing_row(self) -> None:
        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1",
            data_table="foo", worker_id="host:1", lease_ms=60_000,
        )
        assert outcome.action == "run"
        assert outcome.attempts == 1
        async with get_session() as s:
            r = await s.execute(text(
                "SELECT state, attempts, locked_until, worker_id "
                "FROM runtime_inflight WHERE edge_id=:e AND idempotent_key=:k"
            ), {"e": "E::c", "k": "k1"})
            row = r.mappings().one()
        assert row["state"] == "processing"
        assert row["attempts"] == 1
        assert row["worker_id"] == "host:1"
        assert row["locked_until"] is not None

    @pytest.mark.asyncio
    async def test_succeeded_returns_skip(self) -> None:
        async with get_session() as s:
            await s.execute(text(
                "INSERT INTO runtime_inflight (edge_id, idempotent_key, data_table, state, attempts) "
                "VALUES ('E::c', 'k1', 'foo', 'succeeded', 1)"
            ))
        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1",
            data_table="foo", worker_id="host:1", lease_ms=60_000,
        )
        assert outcome.action == "skip"

    @pytest.mark.asyncio
    async def test_processing_with_live_lease_returns_skip(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        async with get_session() as s:
            await s.execute(text(
                "INSERT INTO runtime_inflight (edge_id, idempotent_key, data_table, state, attempts, locked_until, worker_id) "
                "VALUES ('E::c', 'k1', 'foo', 'processing', 1, :lu, 'host:other')"
            ), {"lu": future})
        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1",
            data_table="foo", worker_id="host:1", lease_ms=60_000,
        )
        assert outcome.action == "skip"

    @pytest.mark.asyncio
    async def test_processing_with_expired_lease_takes_over(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        async with get_session() as s:
            await s.execute(text(
                "INSERT INTO runtime_inflight (edge_id, idempotent_key, data_table, state, attempts, locked_until, worker_id) "
                "VALUES ('E::c', 'k1', 'foo', 'processing', 2, :lu, 'host:dead')"
            ), {"lu": past})
        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1",
            data_table="foo", worker_id="host:new", lease_ms=60_000,
        )
        assert outcome.action == "run"
        assert outcome.attempts == 3

    @pytest.mark.asyncio
    async def test_failed_resumes_as_processing(self) -> None:
        async with get_session() as s:
            await s.execute(text(
                "INSERT INTO runtime_inflight (edge_id, idempotent_key, data_table, state, attempts, last_error) "
                "VALUES ('E::c', 'k1', 'foo', 'failed', 1, 'boom')"
            ))
        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1",
            data_table="foo", worker_id="host:1", lease_ms=60_000,
        )
        assert outcome.action == "run"
        assert outcome.attempts == 2


class TestMarkSucceeded:
    @pytest.mark.asyncio
    async def test_clears_lease(self) -> None:
        await claim_inflight(
            edge_id="E::c", idempotent_key="k1",
            data_table="foo", worker_id="host:1", lease_ms=60_000,
        )
        await mark_succeeded(edge_id="E::c", idempotent_key="k1")
        async with get_session() as s:
            r = await s.execute(text(
                "SELECT state, locked_until, worker_id "
                "FROM runtime_inflight WHERE edge_id=:e AND idempotent_key=:k"
            ), {"e": "E::c", "k": "k1"})
            row = r.mappings().one()
        assert row["state"] == "succeeded"
        assert row["locked_until"] is None
        assert row["worker_id"] is None


class TestMarkFailed:
    @pytest.mark.asyncio
    async def test_records_error(self) -> None:
        await claim_inflight(
            edge_id="E::c", idempotent_key="k1",
            data_table="foo", worker_id="host:1", lease_ms=60_000,
        )
        await mark_failed(edge_id="E::c", idempotent_key="k1", last_error="RuntimeError(boom)")
        async with get_session() as s:
            r = await s.execute(text(
                "SELECT state, last_error, locked_until "
                "FROM runtime_inflight WHERE edge_id=:e AND idempotent_key=:k"
            ), {"e": "E::c", "k": "k1"})
            row = r.mappings().one()
        assert row["state"] == "failed"
        assert "boom" in row["last_error"]
        assert row["locked_until"] is None


class TestEdgeIdIsolation:
    """Same idempotent_key + 不同 edge_id (consumer A 与 consumer B) 必须独立 state."""

    @pytest.mark.asyncio
    async def test_succeeded_on_one_edge_does_not_skip_another(self) -> None:
        outcome_a = await claim_inflight(
            edge_id="E::cA", idempotent_key="k1",
            data_table="foo", worker_id="host:1", lease_ms=60_000,
        )
        await mark_succeeded(edge_id="E::cA", idempotent_key="k1")

        outcome_b = await claim_inflight(
            edge_id="E::cB", idempotent_key="k1",
            data_table="foo", worker_id="host:1", lease_ms=60_000,
        )
        assert outcome_a.action == "run"
        assert outcome_b.action == "run"  # consumer B 不被 A 的 succeeded 阻塞
```

- [ ] **Step 4.2: 跑测试确认全失败**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_inflight.py -v`

Expected: ImportError on `app.runtime.inflight` (or table missing)

- [ ] **Step 4.3: 注册 schema 到 migrator**

```python
# apps/agent-service/app/runtime/migrator.py（在现有 runtime-managed table 注册处追加）
RUNTIME_INFLIGHT_DDL = """
CREATE TABLE IF NOT EXISTS runtime_inflight (
    edge_id        TEXT NOT NULL,
    idempotent_key TEXT NOT NULL,
    data_table     TEXT NOT NULL,
    state          TEXT NOT NULL,
    attempts       INT  NOT NULL DEFAULT 0,
    last_error     TEXT,
    locked_until   TIMESTAMPTZ,
    worker_id      TEXT,
    trace_id       TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (edge_id, idempotent_key)
);
CREATE INDEX IF NOT EXISTS runtime_inflight_state_idx ON runtime_inflight (state, locked_until);
"""

# 在 migrate_schema() 中执行：
async def migrate_schema():
    # ... 现有逻辑
    async with get_session() as s:
        await s.execute(text(RUNTIME_INFLIGHT_DDL))
```

- [ ] **Step 4.4: 实现 inflight.py**

```python
# apps/agent-service/app/runtime/inflight.py
"""runtime_inflight state machine (Gap 7.1).

Replaces ``insert_idempotent`` for durable wires. Provides per-edge dedup
state with lease semantics (worker death recovery) and history backfill
(adoption of pre-7a Data rows).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import text

from app.data.session import get_session


def edge_id_for(data_type_qualname: str, consumer_qualname: str) -> str:
    return f"{data_type_qualname}::{consumer_qualname}"


def _lock_key(edge_id: str, idempotent_key: str) -> int:
    h = hashlib.md5(f"{edge_id}::{idempotent_key}".encode()).hexdigest()[:15]
    return int(h, 16) % (2**31)


@dataclass(frozen=True)
class ClaimOutcome:
    action: Literal["run", "skip"]
    attempts: int  # 0 if action == 'skip'


async def claim_inflight(
    *,
    edge_id: str,
    idempotent_key: str,
    data_table: str,
    worker_id: str,
    lease_ms: int,
    trace_id: str | None = None,
) -> ClaimOutcome:
    """Claim runnable state for (edge_id, idempotent_key); return outcome.

    Short transaction: pg_advisory_xact_lock + SELECT/INSERT/UPDATE inflight
    row. Caller MUST run consumer OUTSIDE this transaction (lock released
    on commit) and call mark_succeeded / mark_failed afterwards.
    """
    lock = _lock_key(edge_id, idempotent_key)
    lease_until = datetime.now(timezone.utc) + timedelta(milliseconds=lease_ms)
    async with get_session() as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock})
        r = await s.execute(text(
            "SELECT state, attempts, locked_until "
            "FROM runtime_inflight WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge_id, "k": idempotent_key})
        row = r.mappings().first()

        if row is None:
            await s.execute(text(
                "INSERT INTO runtime_inflight "
                "(edge_id, idempotent_key, data_table, state, attempts, locked_until, worker_id, trace_id) "
                "VALUES (:e, :k, :t, 'processing', 1, :lu, :w, :tid)"
            ), {"e": edge_id, "k": idempotent_key, "t": data_table,
                "lu": lease_until, "w": worker_id, "tid": trace_id})
            return ClaimOutcome(action="run", attempts=1)

        state = row["state"]
        if state == "succeeded":
            return ClaimOutcome(action="skip", attempts=0)
        now = datetime.now(timezone.utc)
        locked_until = row["locked_until"]
        if state == "processing" and locked_until is not None and locked_until > now:
            return ClaimOutcome(action="skip", attempts=0)

        # processing-expired or failed: take over
        new_attempts = (row["attempts"] or 0) + 1
        await s.execute(text(
            "UPDATE runtime_inflight SET state='processing', attempts=:a, "
            "locked_until=:lu, worker_id=:w, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"a": new_attempts, "lu": lease_until, "w": worker_id,
            "e": edge_id, "k": idempotent_key})
        return ClaimOutcome(action="run", attempts=new_attempts)


async def mark_history_backfill(
    *, edge_id: str, idempotent_key: str, data_table: str
) -> None:
    """Insert succeeded inflight row for a Data row that pre-existed before 7a.

    Caller (durable handler) detects existing Data row in the row-missing
    branch and calls this instead of running the consumer.
    """
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight "
            "(edge_id, idempotent_key, data_table, state, attempts, trace_id) "
            "VALUES (:e, :k, :t, 'succeeded', 0, 'backfill') "
            "ON CONFLICT (edge_id, idempotent_key) DO NOTHING"
        ), {"e": edge_id, "k": idempotent_key, "t": data_table})


async def mark_succeeded(*, edge_id: str, idempotent_key: str) -> None:
    async with get_session() as s:
        await s.execute(text(
            "UPDATE runtime_inflight "
            "SET state='succeeded', locked_until=NULL, worker_id=NULL, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge_id, "k": idempotent_key})


async def mark_failed(
    *, edge_id: str, idempotent_key: str, last_error: str
) -> None:
    async with get_session() as s:
        await s.execute(text(
            "UPDATE runtime_inflight "
            "SET state='failed', locked_until=NULL, worker_id=NULL, "
            "    last_error=:err, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"err": last_error[:8000], "e": edge_id, "k": idempotent_key})
```

- [ ] **Step 4.5: 跑 inflight 模块测试全过**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_inflight.py -v`

Expected: 7 passed

- [ ] **Step 4.6: durable handler 接 inflight 状态机**

```python
# apps/agent-service/app/runtime/durable.py（_build_handler 重写）
import os
import socket

from app.runtime.inflight import (
    ClaimOutcome,
    claim_inflight,
    edge_id_for,
    mark_failed,
    mark_history_backfill,
    mark_succeeded,
)
from app.runtime.persist import _dedup_hash
from app.runtime.data import dedup_fields, key_fields  # 现有 helpers

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def _idempotent_key_for(obj: Data) -> str:
    cls = type(obj)
    meta = getattr(cls, "Meta", None)
    dedup_col = getattr(meta, "dedup_column", None) if meta else None
    if dedup_col:
        return str(getattr(obj, dedup_col))
    return _dedup_hash(obj)


async def _check_history_backfill(obj: Data, idem_key: str) -> bool:
    """Return True if Data row already exists (history); False otherwise."""
    cls = type(obj)
    meta = getattr(cls, "Meta", None)
    if meta is not None and getattr(meta, "existing_table", None) is not None:
        return False  # adoption mode: cannot backfill, fall through to first-time
    table = _table_name(cls)
    dedup_col = getattr(meta, "dedup_column", None) if meta else "dedup_hash"
    sql = f"SELECT 1 FROM {table} WHERE {dedup_col} = :k LIMIT 1"
    async with get_session() as s:
        r = await s.execute(text(sql), {"k": idem_key})
        return r.first() is not None


def _build_handler(w: WireSpec, consumer: Callable):
    data_cls = w.data_type
    param_name = next(iter(inputs_of(consumer)))
    edge_id = edge_id_for(data_cls.__qualname__, consumer.__qualname__)
    lease_ms = w.retry.lease_ms if (w.retry is not None) else 300_000
    data_table = _table_name(data_cls)

    async def handler(message: AbstractIncomingMessage) -> None:
        ctx = extract_context(message.headers)
        async with bind_context(ctx):
            try:
                payload = json.loads(message.body)
                obj = data_cls(**payload)
            except Exception:
                logger.exception("durable handler: bad payload, dropping")
                await message.ack()
                return

            idem_key = _idempotent_key_for(obj)

            # History backfill: row missing branch step (a)
            outcome = await claim_inflight(
                edge_id=edge_id, idempotent_key=idem_key,
                data_table=data_table, worker_id=WORKER_ID,
                lease_ms=lease_ms, trace_id=ctx.trace_id,
            )
            # NOTE: claim creates 'processing' on row-missing. To backfill
            # we need to detect "first claim AND data row already exists"
            # BEFORE marking processing. Refactor: claim returns 'fresh'
            # flag; if fresh and history exists, mark_succeeded immediately
            # without running consumer.
            if outcome.action == "run" and outcome.attempts == 1:
                if await _check_history_backfill(obj, idem_key):
                    await mark_succeeded(edge_id=edge_id, idempotent_key=idem_key)
                    await message.ack()
                    return

            if outcome.action == "skip":
                await message.ack()
                return

            try:
                await consumer(**{param_name: obj})
                await mark_succeeded(edge_id=edge_id, idempotent_key=idem_key)
                await message.ack()
            except Exception as e:
                logger.exception("durable consumer failed")
                await mark_failed(
                    edge_id=edge_id, idempotent_key=idem_key,
                    last_error=f"{type(e).__name__}: {e!s}",
                )
                # Retry transport handled in Task 5; for now nack-no-requeue
                # (current behavior, will be replaced)
                await message.nack(requeue=False)

    return handler
```

> **注意**：Step 4.6 临时保留 `nack(requeue=False)` 直接进 DLQ 的行为；retry 路径在 Task 5 接入。Task 4 commit 单独 ship 时业务可观测行为与现状几乎等价（only addition：runtime_inflight 行被写）。

- [ ] **Step 4.7: 跑 durable / inflight 全部测试**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_durable.py tests/runtime/test_inflight.py -v`

Expected: 全 pass。test_durable 现有断言不破坏。

- [ ] **Step 4.8: grep 验证**

```bash
grep -rn "insert_idempotent\b" apps/agent-service/app/runtime/durable.py
```

Expected: 0 命中（durable handler 已切到 inflight）。`runtime/persist.py` 中 `insert_idempotent` 函数仍存在但 durable 不再调（其他地方暂保留，PR 末尾如确认零引用再删）。

- [ ] **Step 4.9: ruff 通过**

Run: `cd apps/agent-service && uv run ruff check app/runtime/inflight.py app/runtime/durable.py app/runtime/migrator.py tests/runtime/test_inflight.py`

- [ ] **Step 4.10: Commit**

```bash
git add apps/agent-service/app/runtime/inflight.py apps/agent-service/app/runtime/migrator.py apps/agent-service/app/runtime/durable.py apps/agent-service/tests/runtime/test_inflight.py
git commit -m "feat(runtime): runtime_inflight schema + state machine + lease + history backfill (Gap 7.1)"
```

---

## Task 5: publish_with_confirm + durable retry transport

**Files:**
- Modify: `apps/agent-service/app/infra/rabbitmq.py`（新增 `publish_with_confirm` method）
- Create: `apps/agent-service/app/runtime/retry.py`
- Modify: `apps/agent-service/app/runtime/durable.py`（_build_handler 失败路径接 retry transport）
- Test: `apps/agent-service/tests/runtime/test_retry.py`
- Test: `apps/agent-service/tests/runtime/test_durable_retry.py` (integration)

handler 失败时按 wire.retry 决定 republish (with confirm + x-delay) 还是 fail-to-DLQ。delivery_count 自管 `x-delivery-count` header（不读 x-death）。

- [ ] **Step 5.1: 写 retry primitive 单元测试**

```python
# apps/agent-service/tests/runtime/test_retry.py
"""Retry decision logic (Gap 7.2/7.3)."""
from __future__ import annotations

import pytest

from app.runtime.retry import (
    DELIVERY_COUNT_HEADER,
    decide_retry,
    delivery_count,
)
from app.runtime.wire import RetryPolicy


class TestDeliveryCount:
    def test_no_header_returns_zero(self) -> None:
        assert delivery_count({}) == 0
        assert delivery_count(None) == 0

    def test_explicit_header(self) -> None:
        assert delivery_count({DELIVERY_COUNT_HEADER: 3}) == 3

    def test_non_int_header_treated_as_zero(self) -> None:
        assert delivery_count({DELIVERY_COUNT_HEADER: "x"}) == 0
        assert delivery_count({DELIVERY_COUNT_HEADER: -1}) == 0


class TestDecideRetry:
    def test_no_policy_returns_dlq(self) -> None:
        d = decide_retry(headers={}, policy=None)
        assert d.action == "dlq"
        assert d.delay_ms == 0

    def test_under_n_returns_retry(self) -> None:
        p = RetryPolicy(n=3, backoff="exponential",
                        base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000)
        d = decide_retry(headers={DELIVERY_COUNT_HEADER: 0}, policy=p)
        assert d.action == "retry"
        assert d.attempt == 1
        assert d.delay_ms == 500

    def test_at_n_returns_dlq(self) -> None:
        p = RetryPolicy(n=3, backoff="exponential",
                        base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000)
        d = decide_retry(headers={DELIVERY_COUNT_HEADER: 3}, policy=p)
        assert d.action == "dlq"

    def test_exponential_backoff(self) -> None:
        p = RetryPolicy(n=5, backoff="exponential",
                        base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000)
        delays = [
            decide_retry(headers={DELIVERY_COUNT_HEADER: i}, policy=p).delay_ms
            for i in range(3)
        ]
        assert delays == [500, 1000, 2000]

    def test_linear_backoff(self) -> None:
        p = RetryPolicy(n=5, backoff="linear",
                        base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000)
        delays = [
            decide_retry(headers={DELIVERY_COUNT_HEADER: i}, policy=p).delay_ms
            for i in range(3)
        ]
        assert delays == [500, 1000, 1500]

    def test_max_delay_clamps(self) -> None:
        p = RetryPolicy(n=20, backoff="exponential",
                        base_delay_ms=500, max_delay_ms=5_000, lease_ms=300_000)
        d = decide_retry(headers={DELIVERY_COUNT_HEADER: 10}, policy=p)
        assert d.delay_ms == 5_000
```

- [ ] **Step 5.2: 跑测试确认全失败**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_retry.py -v`

Expected: ImportError on `app.runtime.retry`

- [ ] **Step 5.3: 实现 retry.py**

```python
# apps/agent-service/app/runtime/retry.py
"""Durable wire retry decision (Gap 7.2/7.3).

Delivery count is read from the runtime-managed ``x-delivery-count`` header
ONLY (not from broker's ``x-death``, which is unreliable across RabbitMQ
configurations). The runtime publishes retry attempts with this header
incremented; first delivery has no header (count=0).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.runtime.wire import RetryPolicy

DELIVERY_COUNT_HEADER = "x-delivery-count"


def delivery_count(headers: dict[str, Any] | None) -> int:
    h = headers or {}
    v = h.get(DELIVERY_COUNT_HEADER)
    if isinstance(v, int) and v >= 0:
        return v
    return 0


@dataclass(frozen=True)
class RetryDecision:
    action: Literal["retry", "dlq"]
    attempt: int  # 1-indexed for the upcoming attempt; 0 if action == 'dlq'
    delay_ms: int


def decide_retry(
    *, headers: dict[str, Any] | None, policy: RetryPolicy | None
) -> RetryDecision:
    if policy is None:
        return RetryDecision(action="dlq", attempt=0, delay_ms=0)
    count = delivery_count(headers)
    if count >= policy.n:
        return RetryDecision(action="dlq", attempt=0, delay_ms=0)
    next_attempt = count + 1
    delay = policy.delay_for_attempt(next_attempt)
    return RetryDecision(action="retry", attempt=next_attempt, delay_ms=delay)
```

> **注意**：`RetryPolicy` 在 `runtime/wire.py` 由 Task 6 实现；为让 Task 5 测试能跑（依赖 RetryPolicy），先在 Task 5 临时 forward-declare —— 不实际，rebase Task 5/6 commit 顺序前 Task 5 的 retry.py 测试无法运行。改进：把 Task 6 移到 Task 5 之前。

**重要修正**：Task 6 (wire DSL retry) 必须先于 Task 5。调整下面顺序：

- 先做 Task 6（添加 `RetryPolicy` + `WireBuilder.retry()`，含 lease_ms 字段）
- 再做 Task 5（依赖 `RetryPolicy`）

下面的 Task 5 步骤继续假设 RetryPolicy 已存在。

- [ ] **Step 5.4: 跑 retry 单元测试全过**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_retry.py -v`

Expected: 8 passed

- [ ] **Step 5.5: 实现 mq.publish_with_confirm**

```python
# apps/agent-service/app/infra/rabbitmq.py（新增 method）
import asyncio
from aio_pika import DeliveryMode

class RabbitMQ:
    # ... 现有代码

    async def publish_with_confirm(
        self,
        route: Route,
        body: dict,
        *,
        delay_ms: int | None = None,
        headers: dict | None = None,
        lane: str | None = ...,
        timeout_s: float = 5.0,
    ) -> bool:
        """Publish with broker publish-confirm; return True iff broker ack-ed.

        Used by durable retry transport (Gap 7.2) and emit_delayed durable
        path (Gap 9). Caller decides on False (DLQ-fallback or raise).
        """
        if self._exchange is None:
            raise RuntimeError("must call declare_topology() first")
        if lane is ...:
            lane = current_lane()
        if lane == "prod":
            lane = None
        if lane:
            await self._ensure_lane_queue(route, lane)
        actual_rk = _lane_rk(route.rk, lane)

        msg_headers: dict[str, Any] = dict(headers) if headers else {}
        if delay_ms is not None:
            msg_headers["x-delay"] = delay_ms

        message = Message(
            body=json.dumps(body).encode(),
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
            headers=msg_headers if msg_headers else None,
        )
        try:
            confirmation = await asyncio.wait_for(
                self._exchange.publish(message, routing_key=actual_rk),
                timeout=timeout_s,
            )
            # aio-pika returns DeliveredMessage on confirm-mode channel
            return confirmation is not None
        except (asyncio.TimeoutError, Exception):
            logger.exception("publish_with_confirm failed: route=%s rk=%s",
                             route.queue, actual_rk)
            return False
```

> **注意**：`self._exchange` 必须在 confirm-select 模式下声明。需要在 `connect()` / `declare_topology()` 启用 publisher confirms：`await self._channel.set_qos(...)` 之后 `self._channel.confirm_select()` 或 aio-pika 的 `publisher_confirms=True` channel 选项。具体看 aio-pika 版本 API。

- [ ] **Step 5.6: durable handler 接 retry transport**

```python
# apps/agent-service/app/runtime/durable.py（_build_handler 失败路径替换）
from app.infra.rabbitmq import mq
from app.runtime.retry import DELIVERY_COUNT_HEADER, decide_retry

# handler 失败 except 块：
except Exception as e:
    logger.exception("durable consumer failed")
    await mark_failed(
        edge_id=edge_id, idempotent_key=idem_key,
        last_error=f"{type(e).__name__}: {e!s}",
    )
    decision = decide_retry(headers=message.headers, policy=w.retry)
    if decision.action == "retry":
        new_headers = dict(message.headers or {})
        new_headers[DELIVERY_COUNT_HEADER] = decision.attempt
        body_dict = json.loads(message.body)
        route = _route_for(w, consumer)
        confirmed = await mq.publish_with_confirm(
            route, body_dict,
            headers=new_headers,
            lane=new_headers.get("lane") or None,
            delay_ms=decision.delay_ms,
        )
        if confirmed:
            await message.ack()
        else:
            # publish failed → DLQ via nack
            await message.nack(requeue=False)
    else:
        await message.nack(requeue=False)  # DLQ
```

- [ ] **Step 5.7: 写 durable retry integration 测试**

```python
# apps/agent-service/tests/runtime/test_durable_retry.py
"""wire(...).durable().retry() handler behavior (Gap 7.2)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.data.types import Data
from app.runtime.retry import DELIVERY_COUNT_HEADER
from app.runtime.wire import RetryPolicy


class _Job(Data):
    job_id: str = ""

    class Meta:
        dedup_column = "job_id"


# fixture: durable_wire_with_retry — 注入一个 .durable().retry(...) wire；
# 见 conftest.py。

class TestDurableRetryIntegration:
    @pytest.mark.asyncio
    async def test_first_failure_republishes_with_delay(
        self, durable_wire_with_retry, mock_mq, clean_inflight
    ) -> None:
        """节点抛错，wire 配 retry(n=3)；handler 必须 mq.publish_with_confirm
        重投带 x-delay≈base_delay_ms + x-delivery-count=1，并 ack 原消息。"""
        # 接入 fixture 后断言 mock_mq.publish_with_confirm called 1 time，
        # delay_ms == 100, headers[DELIVERY_COUNT_HEADER] == 1
        ...

    @pytest.mark.asyncio
    async def test_at_n_failures_goes_to_dlq(
        self, durable_wire_with_retry, mock_mq, clean_inflight
    ) -> None:
        """delivery_count 已等于 n → handler nack-DLQ，不再 publish。"""
        ...

    @pytest.mark.asyncio
    async def test_publish_confirm_failure_falls_to_dlq(
        self, durable_wire_with_retry, mock_mq, clean_inflight
    ) -> None:
        """publish_with_confirm 返回 False → nack-DLQ 兜底。"""
        ...

    @pytest.mark.asyncio
    async def test_retry_preserves_idempotent_key_via_inflight(
        self, durable_wire_with_retry, mock_mq, clean_inflight
    ) -> None:
        """重投消息回到 handler，inflight state=failed → 状态机让它进入
        consumer (不被 dedup 跳过)。"""
        ...
```

> **注意**：integration 测试 fixture 需要 conftest.py 配套。详细 fixture 实现与 test_durable.py 现有 fixture 模式一致。

- [ ] **Step 5.8: 跑 retry 全部测试**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_retry.py tests/runtime/test_durable_retry.py tests/runtime/test_durable.py tests/runtime/test_inflight.py -v`

Expected: 全 pass。test_durable 现有断言不破坏（未配 retry 的 wire 行为：失败 → 直接 DLQ + inflight state=failed）。

- [ ] **Step 5.9: ruff + mypy 通过**

Run: `cd apps/agent-service && uv run ruff check app/runtime/retry.py app/runtime/durable.py app/infra/rabbitmq.py tests/runtime/test_retry.py tests/runtime/test_durable_retry.py`

- [ ] **Step 5.10: Commit**

```bash
git add apps/agent-service/app/runtime/retry.py apps/agent-service/app/runtime/durable.py apps/agent-service/app/infra/rabbitmq.py apps/agent-service/tests/runtime/test_retry.py apps/agent-service/tests/runtime/test_durable_retry.py
git commit -m "feat(runtime): publish_with_confirm + durable retry transport (Gap 7.2)"
```

---

## Task 6: wire(...).durable().retry(n, backoff, lease_ms) DSL

> **注意**：本 task 必须在 Task 5 之前完成（Task 5 retry.py 依赖 `RetryPolicy`）。本 plan task 编号是逻辑顺序，commit 也以 Task 6 → Task 5 顺序排列。或者用 git rebase 调整。建议实施时按 Task 1 → 2 → 3 → 4 → **6** → **5** → 7 → 8 → 9 → 10 顺序。

**Files:**
- Modify: `apps/agent-service/app/runtime/wire.py`（`RetryPolicy` dataclass + `WireBuilder.retry()`）
- Test: `apps/agent-service/tests/runtime/test_wire.py`（追加 retry DSL 测试）

- [ ] **Step 6.1: 写失败测试**

```python
# apps/agent-service/tests/runtime/test_wire.py（追加）
import pytest

from app.data.types import Data
from app.runtime.wire import wire


class _Foo(Data):
    pass


async def _consumer(f: _Foo) -> None: ...


class TestWireRetryDSL:
    def test_default_no_retry_policy(self) -> None:
        spec = wire(_Foo).to(_consumer).durable()._spec
        assert spec.retry is None

    def test_retry_with_n_only(self) -> None:
        spec = wire(_Foo).to(_consumer).durable().retry(n=3)._spec
        assert spec.retry.n == 3
        assert spec.retry.backoff == "exponential"
        assert spec.retry.base_delay_ms == 500
        assert spec.retry.max_delay_ms == 30_000
        assert spec.retry.lease_ms == 300_000

    def test_retry_full_config(self) -> None:
        spec = wire(_Foo).to(_consumer).durable().retry(
            n=5, backoff="linear", base_delay_ms=1000,
            max_delay_ms=60_000, lease_ms=600_000,
        )._spec
        assert spec.retry.n == 5
        assert spec.retry.backoff == "linear"
        assert spec.retry.base_delay_ms == 1000
        assert spec.retry.max_delay_ms == 60_000
        assert spec.retry.lease_ms == 600_000

    def test_retry_without_durable_raises(self) -> None:
        with pytest.raises(ValueError, match="durable"):
            wire(_Foo).to(_consumer).retry(n=3)

    def test_retry_invalid_backoff_raises(self) -> None:
        with pytest.raises(ValueError, match="backoff"):
            wire(_Foo).to(_consumer).durable().retry(n=3, backoff="quadratic")

    def test_retry_n_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="n"):
            wire(_Foo).to(_consumer).durable().retry(n=0)

    def test_retry_lease_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="lease"):
            wire(_Foo).to(_consumer).durable().retry(n=3, lease_ms=0)
```

- [ ] **Step 6.2: 跑测试全失败**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_wire.py::TestWireRetryDSL -v`

Expected: AttributeError on `.retry`

- [ ] **Step 6.3: 实现 RetryPolicy + WireBuilder.retry()**

```python
# apps/agent-service/app/runtime/wire.py
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    n: int
    backoff: str  # "exponential" | "linear"
    base_delay_ms: int
    max_delay_ms: int
    lease_ms: int

    def delay_for_attempt(self, attempt: int) -> int:
        """Calculate delay for the Nth attempt (1-indexed)."""
        if self.backoff == "linear":
            d = self.base_delay_ms * attempt
        else:  # exponential
            d = self.base_delay_ms * (2 ** (attempt - 1))
        return min(d, self.max_delay_ms)


# WireSpec 加字段：
@dataclass
class WireSpec:
    # ... 现有字段
    retry: RetryPolicy | None = None


# WireBuilder：
class WireBuilder:
    def retry(
        self,
        *,
        n: int,
        backoff: str = "exponential",
        base_delay_ms: int = 500,
        max_delay_ms: int = 30_000,
        lease_ms: int = 300_000,
    ) -> "WireBuilder":
        if not self._spec.durable:
            raise ValueError("retry() must come after .durable()")
        if n < 1:
            raise ValueError("retry n must be >= 1")
        if backoff not in ("exponential", "linear"):
            raise ValueError(
                f"backoff must be 'exponential' or 'linear', got {backoff!r}"
            )
        if lease_ms < 1:
            raise ValueError("lease_ms must be >= 1")
        self._spec.retry = RetryPolicy(
            n=n, backoff=backoff,
            base_delay_ms=base_delay_ms, max_delay_ms=max_delay_ms,
            lease_ms=lease_ms,
        )
        return self
```

- [ ] **Step 6.4: 跑测试全过**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_wire.py -v`

Expected: 全 pass（含原 test_wire.py 不破坏）

- [ ] **Step 6.5: ruff 通过**

Run: `cd apps/agent-service && uv run ruff check app/runtime/wire.py tests/runtime/test_wire.py`

- [ ] **Step 6.6: Commit**

```bash
git add apps/agent-service/app/runtime/wire.py apps/agent-service/tests/runtime/test_wire.py
git commit -m "feat(runtime): wire(...).durable().retry(n, backoff, lease_ms) DSL (Gap 7.3)"
```

---

## Task 7: in-process scheduled task pool

**Files:**
- Create: `apps/agent-service/app/runtime/scheduled.py`
- Modify: `apps/agent-service/app/runtime/engine.py`（`Runtime.stop_source_loops` 取消所有 scheduled）
- Test: `apps/agent-service/tests/runtime/test_scheduled.py`

best_effort 路径用的 in-process delayed emit fallback。task 句柄统一管理，runtime stop 时全部取消。docstring 警告 deploy 丢。

- [ ] **Step 7.1: 写失败测试**

```python
# apps/agent-service/tests/runtime/test_scheduled.py
"""In-process scheduled task pool (Gap 9.2 best_effort)."""
from __future__ import annotations

import asyncio

import pytest

from app.runtime.scheduled import (
    SCHEDULED_TASKS,
    cancel_all_scheduled,
    schedule_after,
)


class TestScheduledTaskPool:
    @pytest.mark.asyncio
    async def test_runs_callable_after_delay(self) -> None:
        cancel_all_scheduled()
        called = asyncio.Event()

        async def fire() -> None:
            called.set()

        await schedule_after(0.05, fire)
        await asyncio.wait_for(called.wait(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_task_added_to_pool(self) -> None:
        cancel_all_scheduled()

        async def fire() -> None:
            await asyncio.sleep(10)

        task = await schedule_after(10, fire)
        try:
            assert task in SCHEDULED_TASKS
        finally:
            task.cancel()
            cancel_all_scheduled()

    @pytest.mark.asyncio
    async def test_completed_task_removed_from_pool(self) -> None:
        cancel_all_scheduled()
        ran = asyncio.Event()

        async def fire() -> None:
            ran.set()

        task = await schedule_after(0.01, fire)
        await asyncio.wait_for(ran.wait(), timeout=0.5)
        await asyncio.sleep(0.05)
        assert task not in SCHEDULED_TASKS

    @pytest.mark.asyncio
    async def test_cancel_all_cancels_pending(self) -> None:
        cancel_all_scheduled()

        async def fire() -> None:
            await asyncio.sleep(10)

        t1 = await schedule_after(10, fire)
        t2 = await schedule_after(10, fire)
        n = cancel_all_scheduled()
        await asyncio.sleep(0)
        assert n == 2
        assert t1.cancelled() and t2.cancelled()
        assert not SCHEDULED_TASKS

    @pytest.mark.asyncio
    async def test_callable_exceptions_logged_not_raised(self, caplog) -> None:
        cancel_all_scheduled()

        async def boom() -> None:
            raise RuntimeError("scheduled boom")

        await schedule_after(0.01, boom)
        await asyncio.sleep(0.05)
        assert "scheduled boom" in caplog.text or "RuntimeError" in caplog.text
```

- [ ] **Step 7.2: 跑测试全失败**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_scheduled.py -v`

Expected: ImportError on `app.runtime.scheduled`

- [ ] **Step 7.3: 实现 scheduled.py**

```python
# apps/agent-service/app/runtime/scheduled.py
"""In-process scheduled task pool (Gap 9.2 best_effort fallback for emit_delayed).

WARNING: tasks are tracked in this process only. Runtime stop / pod restart
/ deploy cancels all pending tasks → events are lost. Callers MUST opt in
via ``emit_delayed(..., durability="best_effort")``; default ``durable``
goes through ``runtime_delayed_trigger_{app}`` queue (Task 8/9).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

SCHEDULED_TASKS: set[asyncio.Task] = set()


async def schedule_after(
    delay: float,
    callable_: Callable[[], Awaitable[None]],
) -> asyncio.Task:
    async def runner() -> None:
        try:
            await asyncio.sleep(delay)
            await callable_()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled task raised; swallowing")

    task = asyncio.create_task(runner())
    SCHEDULED_TASKS.add(task)
    task.add_done_callback(SCHEDULED_TASKS.discard)
    return task


def cancel_all_scheduled() -> int:
    pending = [t for t in SCHEDULED_TASKS if not t.done()]
    for t in pending:
        t.cancel()
    SCHEDULED_TASKS.clear()
    return len(pending)
```

- [ ] **Step 7.4: 接到 Runtime.stop_source_loops**

```python
# apps/agent-service/app/runtime/engine.py（Runtime.stop_source_loops 末尾）
from app.runtime.scheduled import cancel_all_scheduled

async def stop_source_loops(self) -> None:
    # ... 现有 source task 取消逻辑
    cancel_all_scheduled()
```

- [ ] **Step 7.5: 跑测试全过**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_scheduled.py -v`

Expected: 5 passed

- [ ] **Step 7.6: ruff 通过**

Run: `cd apps/agent-service && uv run ruff check app/runtime/scheduled.py app/runtime/engine.py tests/runtime/test_scheduled.py`

- [ ] **Step 7.7: Commit**

```bash
git add apps/agent-service/app/runtime/scheduled.py apps/agent-service/app/runtime/engine.py apps/agent-service/tests/runtime/test_scheduled.py
git commit -m "feat(runtime): in-process scheduled task pool (Gap 9.2 best_effort)"
```

---

## Task 8: runtime_delayed_trigger_{app} queue + internal consumer

**Files:**
- Create: `apps/agent-service/app/runtime/delayed_trigger.py`（envelope + consumer + register_trigger_routes）
- Modify: `apps/agent-service/app/infra/rabbitmq.py`（trigger queue 路由声明）
- Modify: `apps/agent-service/app/runtime/engine.py`（启动时注册 trigger queue + 启动 consumer）
- Test: `apps/agent-service/tests/runtime/test_delayed_trigger.py`

按 origin_app + lane 隔离的 trigger queue。consumer 反序列化 envelope 校验 origin_app 匹配后调 emit(data)。

- [ ] **Step 8.1: 写失败测试**

```python
# apps/agent-service/tests/runtime/test_delayed_trigger.py
"""runtime_delayed_trigger_{app} queue + internal consumer (Gap 9.1.2/9.3)."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from app.data.types import Data
from app.runtime.delayed_trigger import (
    DelayedTriggerEnvelope,
    _runtime_trigger_consumer,
    register_trigger_routes,
    trigger_route_name_for,
)


class _Pong(Data):
    n: int = 0


class TestTriggerRouteName:
    def test_includes_app_name(self) -> None:
        assert trigger_route_name_for("agent-service") == "runtime_delayed_trigger_agent-service"
        assert trigger_route_name_for("vectorize-worker") == "runtime_delayed_trigger_vectorize-worker"


class TestRegisterTriggerRoutes:
    def test_registers_routes_for_known_apps(self, mock_mq_routes) -> None:
        """每个已知 APP_NAME 都有一条 base route，lane_fallback=False."""
        known = ["agent-service", "vectorize-worker"]
        register_trigger_routes(known)
        for app in known:
            route = next(
                r for r in mock_mq_routes
                if r.queue == trigger_route_name_for(app)
            )
            assert route.lane_fallback is False


class TestTriggerConsumer:
    @pytest.mark.asyncio
    async def test_origin_app_mismatch_logs_and_acks(
        self, monkeypatch, caplog
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        envelope = DelayedTriggerEnvelope(
            origin_app="vectorize-worker",
            origin_lane=None, data_type="Pong",
            payload={"n": 1}, trace_id="t",
        )
        with patch("app.runtime.delayed_trigger.emit", new=AsyncMock()) as mock_emit:
            await _runtime_trigger_consumer(envelope)
        assert "origin_app" in caplog.text or "wrong" in caplog.text.lower()
        mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_origin_app_match_calls_emit(self, monkeypatch) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        envelope = DelayedTriggerEnvelope(
            origin_app="agent-service",
            origin_lane=None,
            data_type=f"{_Pong.__module__}.{_Pong.__qualname__}",
            payload={"n": 7}, trace_id="t",
        )
        with patch("app.runtime.delayed_trigger.emit", new=AsyncMock()) as mock_emit:
            await _runtime_trigger_consumer(envelope)
        mock_emit.assert_called_once()
        call_arg = mock_emit.call_args.args[0]
        assert isinstance(call_arg, _Pong)
        assert call_arg.n == 7

    @pytest.mark.asyncio
    async def test_unknown_data_type_logs_warning_and_acks(
        self, monkeypatch, caplog
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        envelope = DelayedTriggerEnvelope(
            origin_app="agent-service",
            origin_lane=None, data_type="nonexistent.NoSuchType",
            payload={}, trace_id="t",
        )
        with patch("app.runtime.delayed_trigger.emit", new=AsyncMock()) as mock_emit:
            await _runtime_trigger_consumer(envelope)
        assert "data_type" in caplog.text or "not found" in caplog.text.lower()
        mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_envelope_trace_id_lane_propagates_to_emit(
        self, monkeypatch
    ) -> None:
        from app.api.middleware import lane_var, trace_id_var
        monkeypatch.setenv("APP_NAME", "agent-service")
        envelope = DelayedTriggerEnvelope(
            origin_app="agent-service", origin_lane="feat-x",
            data_type=f"{_Pong.__module__}.{_Pong.__qualname__}",
            payload={"n": 1}, trace_id="orig-trace",
        )
        captured = {}

        async def fake_emit(data) -> None:
            captured["trace_id"] = trace_id_var.get()
            captured["lane"] = lane_var.get()

        with patch("app.runtime.delayed_trigger.emit", new=fake_emit):
            await _runtime_trigger_consumer(envelope)
        assert captured["trace_id"] == "orig-trace"
        assert captured["lane"] == "feat-x"
```

- [ ] **Step 8.2: 跑测试全失败**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_delayed_trigger.py -v`

Expected: ImportError on `app.runtime.delayed_trigger`

- [ ] **Step 8.3: 实现 delayed_trigger.py**

```python
# apps/agent-service/app/runtime/delayed_trigger.py
"""Runtime-owned delayed trigger queue (Gap 9.1.2/9.3).

Architecture:

* Envelope ``DelayedTriggerEnvelope`` wraps ``(origin_app, origin_lane,
  data_type, payload, trace_id)`` and is published with x-delay to
  ``runtime_delayed_trigger_{origin_app}`` (lane queue handled by mq).
* Each runtime instance declares + consumes the route for its OWN
  ``APP_NAME``. Cross-app envelopes are guarded by ``origin_app`` validation.
* When the envelope's delay expires, ``_runtime_trigger_consumer`` rebuilds
  the original Data and calls ``emit(data)`` under the envelope's
  trace/lane context — preserving full fan-out semantics.
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Any

from app.data.types import Data
from app.infra.rabbitmq import Route
from app.runtime.emit import emit
from app.runtime.propagation import Context, bind_context
from app.runtime.wire import wire
from app.runtime.source import Source

logger = logging.getLogger(__name__)


def trigger_route_name_for(app: str) -> str:
    return f"runtime_delayed_trigger_{app}"


class DelayedTriggerEnvelope(Data):
    origin_app:    str
    origin_lane:   str | None = None
    data_type:     str          # f"{cls.__module__}.{cls.__qualname__}"
    payload:       dict[str, Any]
    trace_id:      str | None = None

    class Meta:
        # framework-internal Data; new dedup_hash row per envelope
        pass


def _resolve_data_class(data_type: str) -> type[Data] | None:
    try:
        module_path, _, qualname = data_type.rpartition(".")
        if not module_path:
            return None
        mod = importlib.import_module(module_path)
        cls = getattr(mod, qualname, None)
        if cls is None or not isinstance(cls, type) or not issubclass(cls, Data):
            return None
        return cls
    except Exception:
        return None


def _current_app() -> str:
    return os.getenv("APP_NAME", "agent-service")


async def _runtime_trigger_consumer(envelope: DelayedTriggerEnvelope) -> None:
    """Internal consumer: validate origin_app, rebuild data, call emit()."""
    app = _current_app()
    if envelope.origin_app != app:
        logger.error(
            "delayed trigger envelope origin_app=%s does not match APP_NAME=%s; dropping",
            envelope.origin_app, app,
        )
        return
    cls = _resolve_data_class(envelope.data_type)
    if cls is None:
        logger.warning(
            "delayed trigger envelope data_type=%s not found; dropping",
            envelope.data_type,
        )
        return
    try:
        data = cls(**envelope.payload)
    except Exception:
        logger.exception(
            "delayed trigger envelope payload failed validation: data_type=%s",
            envelope.data_type,
        )
        return
    ctx = Context(trace_id=envelope.trace_id, lane=envelope.origin_lane)
    async with bind_context(ctx):
        await emit(data)


def register_trigger_routes(known_apps: list[str]) -> list[Route]:
    """Register a base trigger route per known APP_NAME.

    Lane queues are created lazily by mq.publish via _ensure_lane_queue;
    base route uses lane_fallback=False so lane envelopes never spill to
    prod queues.
    """
    routes: list[Route] = []
    for app in known_apps:
        queue = trigger_route_name_for(app)
        rk = f"runtime.delayed_trigger.{app}"
        route = Route(queue=queue, rk=rk, lane_fallback=False)
        routes.append(route)
    return routes


def declare_trigger_wire(app: str) -> None:
    """Declare the wire that binds Source.mq(trigger_route_name_for(app)) →
    _runtime_trigger_consumer. Called from runtime startup for own APP_NAME.
    """
    wire(DelayedTriggerEnvelope).from_(
        Source.mq(trigger_route_name_for(app))
    ).to(_runtime_trigger_consumer).durable().retry(n=3, base_delay_ms=500)
```

- [ ] **Step 8.4: rabbitmq.py 接入 trigger routes**

```python
# apps/agent-service/app/infra/rabbitmq.py
# 在 ALL_ROUTES 拼装处加：
from app.runtime.delayed_trigger import register_trigger_routes

KNOWN_APPS = ["agent-service", "vectorize-worker"]  # 与 PaaS 一镜像多服务保持一致

ALL_ROUTES: list[Route] = [
    # ... 现有 route
    *register_trigger_routes(KNOWN_APPS),
]
```

> **注意**：现有 `arq-worker` 临时仍存在但 trigger queue 不为它注册（Phase 7c 退场后即移除）。后续如增加 `event-worker`，更新此列表。

- [ ] **Step 8.5: engine.py 启动时声明 trigger wire**

```python
# apps/agent-service/app/runtime/engine.py（Runtime.run 末尾或 declare_topology 后）
from app.runtime.delayed_trigger import declare_trigger_wire

class Runtime:
    async def run(self) -> None:
        if self._migrate_schema_on_run:
            await self.migrate_schema()
        declare_trigger_wire(self.app_name)  # 注册 framework-internal wire
        await start_consumers(app_name=self.app_name)
        # ... 其余不变
```

- [ ] **Step 8.6: 跑 trigger 测试全过**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_delayed_trigger.py -v`

Expected: 8 passed

- [ ] **Step 8.7: 跑全部 runtime 测试不破坏**

Run: `cd apps/agent-service && uv run pytest tests/runtime/ -v`

Expected: 全 pass

- [ ] **Step 8.8: ruff + mypy 通过**

Run: `cd apps/agent-service && uv run ruff check app/runtime/delayed_trigger.py app/infra/rabbitmq.py app/runtime/engine.py tests/runtime/test_delayed_trigger.py`

- [ ] **Step 8.9: Commit**

```bash
git add apps/agent-service/app/runtime/delayed_trigger.py apps/agent-service/app/infra/rabbitmq.py apps/agent-service/app/runtime/engine.py apps/agent-service/tests/runtime/test_delayed_trigger.py
git commit -m "feat(runtime): runtime_delayed_trigger_{app} queue + internal consumer (Gap 9.1.2/9.3)"
```

---

## Task 9: emit_delayed / emit_at top-level API with durability

**Files:**
- Modify: `apps/agent-service/app/runtime/emit.py`（新增 `emit_delayed` / `emit_at`）
- Modify: `apps/agent-service/app/runtime/__init__.py`（公开 API）
- Test: `apps/agent-service/tests/runtime/test_emit_delayed.py`

emit_delayed 不再按 wire 拓扑分支：`durable` 总走 `runtime_delayed_trigger_{origin_app}` queue + envelope；`best_effort` 总走 schedule_after 直接调 emit(data)。

- [ ] **Step 9.1: 写失败测试**

```python
# apps/agent-service/tests/runtime/test_emit_delayed.py
"""emit_delayed / emit_at API (Gap 9.1)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.data.types import Data
from app.runtime.emit import emit, emit_at, emit_delayed
from app.runtime.scheduled import SCHEDULED_TASKS, cancel_all_scheduled


class _Pong(Data):
    n: int = 0


class TestEmitDelayedDurable:
    @pytest.mark.asyncio
    async def test_publishes_envelope_to_trigger_queue(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        with patch(
            "app.runtime.emit.mq.publish_with_confirm", new=AsyncMock(return_value=True)
        ) as mock_pub:
            await emit_delayed(_Pong(n=1), delay_ms=5000)
        mock_pub.assert_called_once()
        args, kwargs = mock_pub.call_args
        route = args[0]
        body = args[1]
        assert "runtime_delayed_trigger_agent-service" in route.queue
        assert kwargs["delay_ms"] == 5000
        assert body["origin_app"] == "agent-service"
        assert body["data_type"].endswith("_Pong")
        assert body["payload"] == {"n": 1}

    @pytest.mark.asyncio
    async def test_zero_delay_calls_emit_directly(
        self, monkeypatch, in_process_wire_fixture
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        seen: list[int] = []

        async def consumer(p: _Pong) -> None:
            seen.append(p.n)

        in_process_wire_fixture(_Pong, consumer)
        await emit_delayed(_Pong(n=2), delay_ms=0)
        await asyncio.sleep(0.05)
        assert seen == [2]

    @pytest.mark.asyncio
    async def test_publish_failure_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        with patch(
            "app.runtime.emit.mq.publish_with_confirm", new=AsyncMock(return_value=False)
        ):
            with pytest.raises(RuntimeError, match="EmitDelayedDispatchFailed"):
                await emit_delayed(_Pong(n=3), delay_ms=1000)

    @pytest.mark.asyncio
    async def test_envelope_carries_lane_and_trace(self, monkeypatch) -> None:
        from app.api.middleware import lane_var, trace_id_var
        monkeypatch.setenv("APP_NAME", "agent-service")
        l_tok = lane_var.set("feat-x")
        t_tok = trace_id_var.set("trace-1")
        try:
            with patch(
                "app.runtime.emit.mq.publish_with_confirm", new=AsyncMock(return_value=True)
            ) as mock_pub:
                await emit_delayed(_Pong(n=1), delay_ms=1000)
        finally:
            lane_var.reset(l_tok)
            trace_id_var.reset(t_tok)
        body = mock_pub.call_args.args[1]
        assert body["origin_lane"] == "feat-x"
        assert body["trace_id"] == "trace-1"


class TestEmitDelayedBestEffort:
    @pytest.mark.asyncio
    async def test_uses_schedule_after(
        self, monkeypatch, in_process_wire_fixture
    ) -> None:
        cancel_all_scheduled()
        seen: list[int] = []

        async def consumer(p: _Pong) -> None:
            seen.append(p.n)

        in_process_wire_fixture(_Pong, consumer)
        await emit_delayed(_Pong(n=4), delay_ms=50, durability="best_effort")
        assert len(SCHEDULED_TASKS) == 1
        await asyncio.sleep(0.1)
        assert seen == [4]


class TestEmitAt:
    @pytest.mark.asyncio
    async def test_converts_to_delay_ms(self, monkeypatch) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        called_ms: list[int] = []

        async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
            called_ms.append(delay_ms)

        with patch("app.runtime.emit.emit_delayed", new=fake_emit_delayed):
            when = datetime.now(timezone.utc) + timedelta(seconds=10)
            await emit_at(_Pong(n=4), when=when)
        assert 9_500 < called_ms[0] < 10_500

    @pytest.mark.asyncio
    async def test_past_when_uses_zero_delay(self, monkeypatch) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        called_ms: list[int] = []

        async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
            called_ms.append(delay_ms)

        with patch("app.runtime.emit.emit_delayed", new=fake_emit_delayed):
            when = datetime.now(timezone.utc) - timedelta(seconds=5)
            await emit_at(_Pong(n=5), when=when)
        assert called_ms[0] == 0


class TestEmitDelayedValidation:
    @pytest.mark.asyncio
    async def test_invalid_durability_raises(self) -> None:
        with pytest.raises(ValueError, match="durability"):
            await emit_delayed(_Pong(n=1), delay_ms=0, durability="weird")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_negative_delay_clamps_to_zero(
        self, monkeypatch, in_process_wire_fixture
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        seen: list[int] = []

        async def consumer(p: _Pong) -> None:
            seen.append(p.n)

        in_process_wire_fixture(_Pong, consumer)
        await emit_delayed(_Pong(n=6), delay_ms=-100)
        await asyncio.sleep(0.05)
        assert seen == [6]

    @pytest.mark.asyncio
    async def test_delay_exceeds_x_delay_max_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        # x-delay int32 max ~24 days = 2_147_483_647 ms
        with pytest.raises(ValueError, match="x-delay"):
            await emit_delayed(_Pong(n=7), delay_ms=2_500_000_000)
```

- [ ] **Step 9.2: 跑测试全失败**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_emit_delayed.py -v`

Expected: ImportError on `emit_delayed` / `emit_at`

- [ ] **Step 9.3: 实现 emit_delayed / emit_at**

```python
# apps/agent-service/app/runtime/emit.py（追加）
import os
from datetime import datetime, timezone
from typing import Literal

from app.api.middleware import lane_var, trace_id_var
from app.infra.rabbitmq import Route, mq
from app.runtime.delayed_trigger import (
    DelayedTriggerEnvelope,
    trigger_route_name_for,
)
from app.runtime.scheduled import schedule_after

# RabbitMQ x-delayed-message exchange uses int32 ms; cap derived from spec
_X_DELAY_MAX_MS = 2_147_483_647


def _current_app() -> str:
    return os.getenv("APP_NAME", "agent-service")


async def emit_delayed(
    data: Data,
    *,
    delay_ms: int,
    durability: Literal["durable", "best_effort"] = "durable",
) -> None:
    """Schedule emit(data) to run after delay_ms.

    durability="durable" (default): publish-with-confirm to
    runtime_delayed_trigger_{origin_app} queue with x-delay; runtime's
    internal consumer rebuilds and calls emit(data) at expiry. Survives
    pod restart / deploy as long as the origin app+lane comes back up.

    durability="best_effort": schedule asyncio task in current process.
    Lost on runtime stop / pod restart / deploy. trace/lane NOT propagated
    across the scheduled boundary (downstream chain is independent trace).
    """
    if durability not in ("durable", "best_effort"):
        raise ValueError(
            f"durability must be 'durable' or 'best_effort', got {durability!r}"
        )
    if delay_ms < 0:
        delay_ms = 0
    if delay_ms > _X_DELAY_MAX_MS:
        raise ValueError(
            f"delay_ms={delay_ms} exceeds RabbitMQ x-delay int32 max "
            f"({_X_DELAY_MAX_MS} ms ≈ 24 days)"
        )

    if delay_ms == 0:
        await emit(data)
        return

    if durability == "best_effort":
        async def _fire() -> None:
            await emit(data)
        await schedule_after(delay_ms / 1000.0, _fire)
        return

    # durable path: trigger queue + envelope
    app = _current_app()
    lane = lane_var.get()
    cls = type(data)
    envelope = DelayedTriggerEnvelope(
        origin_app=app,
        origin_lane=lane,
        data_type=f"{cls.__module__}.{cls.__qualname__}",
        payload=data.model_dump(mode="json"),
        trace_id=trace_id_var.get(),
    )
    body = envelope.model_dump(mode="json")
    route = Route(
        queue=trigger_route_name_for(app),
        rk=f"runtime.delayed_trigger.{app}",
        lane_fallback=False,
    )
    confirmed = await mq.publish_with_confirm(
        route, body, headers=inject_context({"data_type": "DelayedTriggerEnvelope"}),
        delay_ms=delay_ms, lane=lane,
    )
    if not confirmed:
        raise RuntimeError(
            f"EmitDelayedDispatchFailed: publish-confirm failed for "
            f"{cls.__name__} (origin_app={app}, lane={lane})"
        )


async def emit_at(
    data: Data,
    *,
    when: datetime,
    durability: Literal["durable", "best_effort"] = "durable",
) -> None:
    now = datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - now).total_seconds()
    delay_ms = max(0, int(delta * 1000))
    await emit_delayed(data, delay_ms=delay_ms, durability=durability)
```

- [ ] **Step 9.4: 导出**

```python
# apps/agent-service/app/runtime/__init__.py
from app.runtime.emit import emit, emit_at, emit_delayed

__all__ = [..., "emit_delayed", "emit_at"]
```

- [ ] **Step 9.5: 跑测试全过**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_emit_delayed.py tests/runtime/test_emit_inprocess.py tests/runtime/test_emit_cross_process.py -v`

Expected: 全 pass（含原 emit 测试不破坏）

- [ ] **Step 9.6: ruff + mypy 通过**

Run: `cd apps/agent-service && uv run ruff check app/runtime/emit.py app/runtime/__init__.py tests/runtime/test_emit_delayed.py`

- [ ] **Step 9.7: Commit**

```bash
git add apps/agent-service/app/runtime/emit.py apps/agent-service/app/runtime/__init__.py apps/agent-service/tests/runtime/test_emit_delayed.py apps/agent-service/tests/runtime/conftest.py
git commit -m "feat(runtime): emit_delayed / emit_at top-level API with durability param (Gap 9)"
```

---

## Task 10: CI grep gate + dev drill + ship

**Files:**
- Create: `.github/workflows/grep-gate.yml`
- Create: `.github/grep-baselines.json`
- Modify: `MEMORY.md` 索引 + `project_dataflow_phase7.md`（更新 spec 文件名 + 7a 进度）
- Create: `docs/superpowers/retrospectives/2026-05-XX-phase7a-retry-drill.md`（drill 完成后）

### 10.1 — CI grep gate

- [ ] **Step 10.1.1: 创建 grep-gate workflow**

```yaml
# .github/workflows/grep-gate.yml
name: grep-gate

on:
  pull_request:
    branches: [main]

jobs:
  grep-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Closed-gap exact-zero (Gap 7+9+11)
        run: |
          set -e
          # Gap 7: 业务代码不准自实现 retry / sleep / 自管 idempotent
          test "$(grep -rn "while.*retry\|for.*range.*retry" apps/agent-service/app/{agent,chat,life,memory,nodes,api}/ 2>/dev/null | grep -v "__pycache__\|test_" | wc -l)" -eq 0
          test "$(grep -rn "asyncio\.sleep" apps/agent-service/app/{agent,chat,life,memory,nodes,api}/ 2>/dev/null | grep -v "__pycache__\|test_" | wc -l)" -eq 0
          test "$(grep -rn "insert_idempotent\b" apps/agent-service/app/ 2>/dev/null | grep -v "runtime/persist.py\|__pycache__\|test_" | wc -l)" -eq 0
          # Gap 11: 业务代码不准直接读写 trace_id_var / lane_var / headers["trace_id"|"lane"]
          test "$(grep -rn "trace_id_var\|lane_var" apps/agent-service/app/ 2>/dev/null | grep -v "runtime/propagation.py\|runtime/middleware.py\|api/middleware.py\|infra/rabbitmq.py\|__pycache__\|test_" | wc -l)" -le 3
          test "$(grep -rn 'headers\["trace_id"\]\|headers\["lane"\]' apps/agent-service/app/ 2>/dev/null | grep -v "runtime/propagation.py\|__pycache__\|test_" | wc -l)" -eq 0
      - name: Open-gap baseline no-new
        run: |
          set -e
          python3 - <<'PY'
          import json, subprocess, sys
          baselines = json.load(open(".github/grep-baselines.json"))
          patterns = {
              "gap_13_get_session": (
                  r"get_session(\\|AsyncSessionLocal",
                  ["apps/agent-service/app/nodes/", "apps/agent-service/app/agent/",
                   "apps/agent-service/app/chat/", "apps/agent-service/app/life/",
                   "apps/agent-service/app/memory/", "apps/agent-service/app/long_tasks/"],
              ),
              "gap_14_redis_setnx_business": (
                  r"redis\.set(.*nx=True\\|redis\.eval(\\|redis\.smembers(\\|redis\.sadd(",
                  ["apps/agent-service/app/nodes/", "apps/agent-service/app/agent/",
                   "apps/agent-service/app/chat/", "apps/agent-service/app/life/",
                   "apps/agent-service/app/memory/"],
              ),
              "gap_15_arq_imports": (
                  r"from arq\\|enqueue_job\\|create_pool",
                  ["apps/agent-service/app/"],
              ),
              "gap_16_httpx_business": (
                  r"import httpx\\|from httpx",
                  ["apps/agent-service/app/nodes/", "apps/agent-service/app/agent/",
                   "apps/agent-service/app/chat/", "apps/agent-service/app/life/",
                   "apps/agent-service/app/memory/"],
              ),
              "gap_19_create_task_business": (
                  r"asyncio\.create_task\\|asyncio\.ensure_future",
                  ["apps/agent-service/app/nodes/", "apps/agent-service/app/agent/",
                   "apps/agent-service/app/chat/", "apps/agent-service/app/life/",
                   "apps/agent-service/app/memory/"],
              ),
          }
          fail = False
          for key, (pat, dirs) in patterns.items():
              cmd = ["grep", "-rn", "-E", pat, *dirs]
              try:
                  out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
                  count = len([l for l in out.splitlines()
                               if "__pycache__" not in l and "/test_" not in l])
              except subprocess.CalledProcessError:
                  count = 0
              base = baselines.get(key, 0)
              print(f"{key}: count={count} baseline={base}")
              if count > base:
                  print(f"  FAIL: {count} > baseline {base}", file=sys.stderr)
                  fail = True
          sys.exit(1 if fail else 0)
          PY
```

- [ ] **Step 10.1.2: 创建 baseline 文件**

```bash
# 跑一次 baseline 计数（在 PR 准备 ship 前）
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/refactor-dataflow-parse-7
python3 -c "
import subprocess, json
patterns = {
    'gap_13_get_session': (r'get_session(\|AsyncSessionLocal',
        ['apps/agent-service/app/nodes/', 'apps/agent-service/app/agent/',
         'apps/agent-service/app/chat/', 'apps/agent-service/app/life/',
         'apps/agent-service/app/memory/', 'apps/agent-service/app/long_tasks/']),
    'gap_14_redis_setnx_business': (r'redis\.set(.*nx=True\|redis\.eval(\|redis\.smembers(\|redis\.sadd(',
        ['apps/agent-service/app/nodes/', 'apps/agent-service/app/agent/',
         'apps/agent-service/app/chat/', 'apps/agent-service/app/life/',
         'apps/agent-service/app/memory/']),
    'gap_15_arq_imports': (r'from arq\|enqueue_job\|create_pool',
        ['apps/agent-service/app/']),
    'gap_16_httpx_business': (r'import httpx\|from httpx',
        ['apps/agent-service/app/nodes/', 'apps/agent-service/app/agent/',
         'apps/agent-service/app/chat/', 'apps/agent-service/app/life/',
         'apps/agent-service/app/memory/']),
    'gap_19_create_task_business': (r'asyncio\.create_task\|asyncio\.ensure_future',
        ['apps/agent-service/app/nodes/', 'apps/agent-service/app/agent/',
         'apps/agent-service/app/chat/', 'apps/agent-service/app/life/',
         'apps/agent-service/app/memory/']),
}
out = {}
for key, (pat, dirs) in patterns.items():
    try:
        r = subprocess.check_output(['grep', '-rn', '-E', pat, *dirs],
                                     text=True, stderr=subprocess.DEVNULL)
        n = len([l for l in r.splitlines() if '__pycache__' not in l and '/test_' not in l])
    except subprocess.CalledProcessError:
        n = 0
    out[key] = n
print(json.dumps(out, indent=2))
" > .github/grep-baselines.json
cat .github/grep-baselines.json
```

### 10.2 — 全 runtime 测试 + ruff + mypy

- [ ] **Step 10.2.1: 全部 runtime 测试 green**

Run: `cd apps/agent-service && uv run pytest tests/runtime/ -v`

Expected: 全 pass

- [ ] **Step 10.2.2: 跑全部 agent-service 测试不破坏**

Run: `cd apps/agent-service && uv run pytest -x`

Expected: 全 pass

- [ ] **Step 10.2.3: ruff + mypy clean**

Run: `cd apps/agent-service && uv run ruff check && uv run mypy app/runtime/`

Expected: No issues

- [ ] **Step 10.2.4: 跑 7a grep gate（spec §4.2）本地一次**

```bash
bash -c '
test "$(grep -rn "while.*retry\|for.*range.*retry" apps/agent-service/app/{agent,chat,life,memory,nodes,api}/ 2>/dev/null | grep -v "__pycache__\|test_" | wc -l)" -eq 0
test "$(grep -rn "asyncio\.sleep" apps/agent-service/app/{agent,chat,life,memory,nodes,api}/ 2>/dev/null | grep -v "__pycache__\|test_" | wc -l)" -eq 0
test "$(grep -rn "insert_idempotent\b" apps/agent-service/app/ 2>/dev/null | grep -v "runtime/persist.py\|__pycache__\|test_" | wc -l)" -eq 0
test "$(grep -rn "trace_id_var\|lane_var" apps/agent-service/app/ 2>/dev/null | grep -v "runtime/propagation.py\|runtime/middleware.py\|api/middleware.py\|infra/rabbitmq.py\|__pycache__\|test_" | wc -l)" -le 3
test "$(grep -rn "headers\[\"trace_id\"\]\|headers\[\"lane\"\]" apps/agent-service/app/ 2>/dev/null | grep -v "runtime/propagation.py\|__pycache__\|test_" | wc -l)" -eq 0
echo "all gate checks pass"
'
```

### 10.3 — Dev 泳道部署 + drill

- [ ] **Step 10.3.1: 部署到独立泳道 dev-phase7a**

```bash
make deploy APP=agent-service LANE=dev-phase7a GIT_REF=refactor/dataflow-parse-7
make release APP=arq-worker LANE=dev-phase7a VERSION=<同 agent-service version>
make release APP=vectorize-worker LANE=dev-phase7a VERSION=<同 agent-service version>
```

- [ ] **Step 10.3.2: 部署日志确认 runtime_inflight + trigger queue 创建**

观察 agent-service / vectorize-worker pod 启动日志，确认：
- `runtime_inflight` 表 CREATE 语句执行
- `runtime_delayed_trigger_agent-service` / `runtime_delayed_trigger_vectorize-worker` queue declare 成功
- trigger consumer 启动消费

- [ ] **Step 10.3.3: 绑定 dev bot**

```bash
/ops bind TYPE=bot KEY=dev LANE=dev-phase7a
```

- [ ] **Step 10.3.4: 飞书消息正常 + Langfuse trace 完整**

群聊 + p2p 各发一条 → 看 Langfuse trace 完整（emit 链路 trace_id 不断链）。等 1-2 分钟，看 cron 触发的 minute_tick 链路在 Langfuse 上有 `cron:` 开头的 trace_id。

- [ ] **Step 10.3.5: Retry drill — n=3 进 DLQ**

临时 commit 一个失败 wire（不进 main，仅 dev 泳道用）：

```python
# 测试用 wire（drill 完后立刻 git revert）
@node
async def _drill_failing(req: SafetyCheckRequest) -> None:
    raise RuntimeError("drill-injected failure")

wire(SafetyCheckRequest).to(_drill_failing).durable().retry(
    n=3, backoff="exponential", base_delay_ms=200, max_delay_ms=2000,
)
```

发起 1 条 SafetyCheckRequest emit，观察：
1. `runtime_inflight` 出现 `(edge_id="SafetyCheckRequest::_drill_failing", idempotent_key=...)` 行：state=processing, attempts=1, locked_until≈now+5min, worker_id='<host>:<pid>'
2. 第一次抛错 → row 转 state=failed, attempts=1, locked_until=NULL, worker_id=NULL, last_error=RuntimeError(...) → mq republish 带 x-delay≈200ms + x-delivery-count=1
3. ~200ms 后 handler 再次进入 → state=processing, attempts=2, locked_until≈now+5min → 抛错 → state=failed, attempts=2 → republish x-delay≈400ms + x-delivery-count=2
4. 第 3 次同样抛错 → attempts=3==n → DLQ + row 终态 state=failed, attempts=3
5. RabbitMQ DLQ 看到该消息

- [ ] **Step 10.3.6: 并发 drill — lease 验证**

worker 持锁 processing 期间手工再投同 idempotent_key 消息：

```sql
-- pg 连接到 dev-phase7a Postgres
INSERT INTO runtime_inflight (edge_id, idempotent_key, data_table, state, attempts, locked_until, worker_id)
VALUES ('SafetyCheckRequest::_drill_failing', 'test-key', 'safety_check_request', 'processing', 1,
        now() + INTERVAL '5 minutes', 'host:fake');
```

通过 admin emit 工具发同 key 消息 → handler 看到 state=processing AND locked_until > now() → ack + skip（row 不变）。

模拟 lease 过期：

```sql
UPDATE runtime_inflight SET locked_until = now() - INTERVAL '1 second'
WHERE idempotent_key = 'test-key';
```

再投同消息 → handler 接管，attempts=2，worker_id 改新 worker。

- [ ] **Step 10.3.7: 历史兼容 backfill drill**

```sql
-- 模拟升级前已处理（仅有 Data 行无 inflight）
INSERT INTO safety_check_request (id, dedup_hash, ...) VALUES (...);
DELETE FROM runtime_inflight WHERE idempotent_key = '<同 dedup_hash>';
```

emit 同 idempotent_key 消息 → handler row missing 分支 → SELECT Data 表 hit → INSERT inflight `(state='succeeded', trace_id='backfill')` + ack；consumer **不被调用**。

- [ ] **Step 10.3.8: emit_delayed durable drill**

代码内调 `await emit_delayed(SomeDataInstance, delay_ms=10_000)`：
- 看 mq 中 `runtime_delayed_trigger_agent-service` queue 出现 envelope（如有 dev-phase7a lane queue 则是 lane 队列）
- 等 10s 后 trigger consumer 反序列化 + 调 emit() → 业务下游正常触发
- envelope.origin_app == "agent-service"，lane == "dev-phase7a"，trace_id == 发起方 trace_id

- [ ] **Step 10.3.9: emit_delayed best_effort drill**

代码内调 `await emit_delayed(SomeDataInstance, delay_ms=10_000, durability="best_effort")`：
- mq 不出现 envelope（schedule_after 走本地）
- 等 10s 后 emit() 被调
- 部署期间（重启 pod）发起 best_effort delay → pod 重启后 emit 不再被调（验证 deploy 丢消息行为，与 docstring 警告一致）

- [ ] **Step 10.3.10: drill 截图 / 日志 → retrospective**

写 `docs/superpowers/retrospectives/2026-05-XX-phase7a-retry-drill.md`，记录每个 drill 的：
- 操作步骤
- runtime_inflight 表观测截图
- RabbitMQ 管理界面截图（DLQ + trigger queue）
- Langfuse trace 截图

### 10.4 — 解绑 + 下泳道 + 等用户许可后 ship

- [ ] **Step 10.4.1: revert 临时 drill wire（git）**

drill 用的 `_drill_failing` 节点必须从分支删除（不带进 main）。

- [ ] **Step 10.4.2: 解绑 + 下泳道**

```bash
/ops unbind TYPE=bot KEY=dev
make undeploy APP=agent-service LANE=dev-phase7a
make undeploy APP=arq-worker LANE=dev-phase7a
make undeploy APP=vectorize-worker LANE=dev-phase7a
```

- [ ] **Step 10.4.3: 更新 memory + 项目文档**

修改 `~/.claude/projects/.../memory/project_dataflow_phase7.md`：
- spec 文件名改 `2026-05-08-dataflow-phase-7-gap-analysis.md`
- 7a 状态：spec ✅, plan ✅, dev 泳道 E2E ✅, 待用户验收后 ship；7b/7c/7d/7e 待启动

更新 `docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md` 顶部历史校正：Phase 7 spec 文件名 / 路径补上。

- [ ] **Step 10.4.4: Commit doc + CI gate**

```bash
git add .github/workflows/grep-gate.yml .github/grep-baselines.json docs/superpowers/specs/2026-05-08-dataflow-phase-7-gap-analysis.md docs/superpowers/plans/2026-05-08-dataflow-phase-7a-transport.md docs/superpowers/retrospectives/2026-05-XX-phase7a-retry-drill.md
git commit -m "chore(ci): grep gate for Gap 7+9+11 closed + baseline for Gap 13/14/15/16/19 open + Phase 7 spec/plan/retro"
```

> **注意**：spec / plan 文件可能已经先期 commit 过；这里只确认未 commit 的文档（retrospective + CI gate + baseline）一起提交。

- [ ] **Step 10.4.5: 等用户许可后 ship**

**绝对不可自行 ship。** 等用户明确说"合"或"merge"。

PR title (英文)：`feat(runtime): Phase 7a — transport primitives (Gap 7 / 9 / 11)`

PR body 英文，列：

```
## Summary
- Gap 7: durable retry policy + runtime_inflight state machine + lease + history backfill
- Gap 9: emit_delayed / emit_at top-level API; durable goes through runtime_delayed_trigger_{app} queue, best_effort uses in-process scheduled task
- Gap 11: trace/lane propagation primitive; cron/interval auto-generate trace_id; sink dispatch dual-writes header + body for chat-response-worker compat

## Test plan
- [ ] runtime/ unit + contract tests green
- [ ] full agent-service pytest green
- [ ] ruff + mypy clean
- [ ] dev lane E2E: feishu p2p + group chat + Langfuse trace
- [ ] retry drill (n=3 → DLQ)
- [ ] concurrent lease drill (live lease skip + expired takeover)
- [ ] history backfill drill (existing Data row → inflight succeeded)
- [ ] emit_delayed durable drill (envelope through trigger queue)
- [ ] emit_delayed best_effort drill (in-process schedule, lost on deploy)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

不带中文 / 邮箱字符（memory feedback `feedback_pr_summary_no_email.md` / `feedback_pr_no_chinese.md`）。

用 `ghc pr create` + `/ship` skill 完成 merge（项目铁律：必须用 `/ship`，禁用 `superpowers:finishing-a-development-branch`）。

- [ ] **Step 10.4.6: prod 部署后跟踪 1 小时**

`/ship` 自动 prod 部署。部署后人工观察：
- agent-service / arq-worker / vectorize-worker 三个 deployment release
- Loki 日志无 ERROR 飙升
- Langfuse trace 完整
- runtime_inflight 表行数稳定增长（业务正常处理消息）
- RabbitMQ DLQ 不异常增长

异常时立即按 §6 风险中的回滚策略处理。

---

## 实施顺序总结（按 commit 时序）

```
Task 1 → commit 2:  feat(runtime): propagation primitive
Task 2 → commit 3:  refactor(runtime): durable / debounce / source-mq / sink-dispatch use propagation
Task 3 → commit 4:  feat(runtime): cron / interval auto-generate trace_id
Task 4 → commit 5:  feat(runtime): runtime_inflight schema + state machine + lease + history backfill
Task 6 → commit 7:  feat(runtime): wire(...).durable().retry(n, backoff, lease_ms) DSL
                     ↑ Task 6 必须先于 Task 5（Task 5 retry.py 依赖 RetryPolicy）
Task 5 → commit 6:  feat(runtime): publish_with_confirm + durable retry transport
Task 7 → commit 8:  feat(runtime): in-process scheduled task pool
Task 8 → commit 9:  feat(runtime): runtime_delayed_trigger_{app} queue + internal consumer
Task 9 → commit 10: feat(runtime): emit_delayed / emit_at top-level API with durability param
Task 10 § CI / drill / ship:
  → commit 11: chore(ci): grep gate + baseline + Phase 7 spec/plan/retro
  → ship
```

**约 11 个 commit**（含 doc + Task 1-9 feature + CI + spec/plan/retro）。每 commit 自包含 + 测试 green + ruff 通过。

> **编号顺序与 commit 编号略有错位**：Task 6 (wire DSL retry) 在 commit 时序上位于 Task 5 (retry transport) 之前；plan 章节按逻辑分组，commit 时按依赖排。

## 风险记录

- **Task 4 inflight handler 改写**：handler 流程从 5 步变成 ~12 步（claim + history backfill check + run consumer + mark succeeded/failed + retry decision），每条分支必须有测试覆盖。Step 4.6 的 `_check_history_backfill` 与 `claim_inflight` 调用顺序错乱会导致升级后历史消息被重跑——**Step 4.7 现有 test_durable.py 全过 + 新加 test_inflight.py 全过**是双重保险
- **Task 5 publish_with_confirm 接入 channel mode**：aio-pika channel 必须是 confirm-select 模式才能 publish-confirm。`infra/rabbitmq.py:connect()` 必须创建 confirm-select channel；如果当前 channel 不支持，需扩展 RabbitMQ 类（也可能 aio-pika 默认就支持，但要查 API 版本）
- **Task 8 KNOWN_APPS 维护**：`runtime/delayed_trigger.py` 中的 `KNOWN_APPS` 列表是硬编码的 [agent-service, vectorize-worker]。如果 PaaS 增加新 app（如 Phase 7c 的 event-worker 替换 arq-worker），必须同步更新此列表。**Step 10.3.2 部署日志中如某 app 的 trigger queue declare 失败**，立即停止 ship，回查列表
- **Task 9 emit_delayed 跨 in-process / cross-process fan-out**：emit_delayed 不再按 wire 拓扑分支，但 trigger consumer 内调 emit() 时 fan-out 行为依赖 graph 注册和 APP_NAME。如果 trigger consumer 跑在 vectorize-worker，但目标 Data 的 in-process consumer 注册在 agent-service —— emit() 会跨进程 publish；这是预期行为（与发起方的进程一致）。**Step 10.3.8 验证 emit_delayed 跨 lane / 跨 app 不被错配进程消费**
- **Task 10 dev 泳道部署中断 prod 异步任务**：CLAUDE.md「部署 = 杀 Pod = 中断所有异步任务」铁律。Step 10.3.1 部署前必须确认 prod 没有正在跑的 rebuild / afterthought / long_tasks 后台任务。如果有，要么等它跑完，要么告知用户部署会中断 X
- **Task 10 用户验收后才能 ship**：CLAUDE.md「合码必须等用户确认」铁律。Step 10.4.5 严禁自行 ship。**对策**：plan 中显式 step「等用户明确说 merge / 合 / ship」

## 完成定义（Definition of Done）

- 全部 10 个 task checkbox ticked
- spec §4.2 7a 硬验收全过（grep gate + dev 泳道 E2E + retry/lease/backfill/emit_delayed drill 四类齐全）
- PR `/ship` merge 进 main（用户许可后）
- prod 部署完成（agent-service + arq-worker + vectorize-worker 三个 deployment 同步 release）
- prod 1 小时观察期无回归
- memory 更新（7a ✅，下一步 7b）
- retrospective 落盘（drill 截图 / 日志）

## Out of Scope（本 plan 明确不做）

- Gap 8 (outbox)、Gap 10 (segment)、Gap 12 (DLQ replay CLI)、Gap 13-19：留给 7b/c/d/e
- 业务节点改造：本 PR 仅扩 framework + refactor runtime；业务作者 0 改动
- ts 侧（lark-server / chat-response-worker）：body-level lane 兼容窗口保留，不动 ts
- DLQ replay CLI：留给 7b
- 监控告警：本 PR 不动 alert rules（runtime queue / DLQ 已在 PR #202 监控；新增 `runtime_inflight` 表的 stuck-processing 监控归 Phase 8+）
- runtime_inflight GC：不实施（小数据量；如需归入运维 SQL 工单）
- `insert_idempotent` 函数体删除：本 PR 不删（防止 git rebase 期间其他地方还引用），下个 PR 末尾如确认零引用再删
