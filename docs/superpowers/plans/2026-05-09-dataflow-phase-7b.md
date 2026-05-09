# Dataflow Phase 7b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Phase 7b — outbox pattern (Gap 8) + DLQ replay tooling (Gap 12) + typed error policy DSL (Gap 18) — closing the dataflow runtime reliability surface.

**Architecture:**
- Gap 18 adds `wire(...).on_error("dlq" | "ignore-duplicate" | "manual-review")` + `DuplicateData` / `NeedsReview` typed exceptions; durable handler routes via single-except + `_route_consumer_exception` helper with `handled→return / dlq→raise` contract; `claim_inflight` adds `review` to skip-terminal states.
- Gap 12 adds `delete_inflight` (per-mode semantics: `edge_idempotent` raises `AlreadySucceededError` on succeeded targets, `trace_id` deletes non-succeeded only) + 4 admin HTTP endpoints + `runtime_dlq_audit` table + 6-step transaction-like requeue protocol + Makefile targets + runbook.
- Gap 8 adds `runtime_outbox` table + `transactional_emit` / `OutboxEmitter` + dispatcher loop calling `emit(data)` for full wire fan-out; SELECT filters by `(origin_app, lane)` (NULL-safe); 8 mutation nodes migrate from "commit-then-emit" to in-transaction outbox append.

**Tech Stack:** Python 3.x / SQLAlchemy AsyncSession / aio-pika / pydantic / pytest / FastAPI. Schema lives in DDL lists (no alembic — `runtime/migrator.py` model). All commits must keep ruff + mypy + pytest green.

**Spec reference:** `docs/superpowers/specs/2026-05-08-dataflow-phase-7b-design.md` (v5)

**Branch:** `refactor/flow-parse-7b` (commit #1 spec already shipped at `ba4eab3` + `90d2410` + `72df82c`).

**Commit-task mapping:** spec §5 lists 12 commits; commit #1 (spec) is done. This plan implements #2-#12 as Tasks 1-11.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/agent-service/app/runtime/errors.py` | **Create** | `DuplicateData` / `NeedsReview` / `AlreadySucceededError` exception classes |
| `apps/agent-service/app/runtime/wire.py` | Modify | `WireSpec.on_error: str` field + `WireBuilder.on_error()` method |
| `apps/agent-service/app/runtime/durable.py` | Modify | Replace lines 217-271 with single-except + `_route_consumer_exception` helper |
| `apps/agent-service/app/runtime/inflight.py` | Modify | Add `review` to skip-terminal in `claim_inflight`; add `mark_review`, `delete_inflight`, `DeleteOutcome` |
| `apps/agent-service/app/runtime/review_queue.py` | **Create** | `publish_to_review_queue` + per-(data, consumer) review queue declaration |
| `apps/agent-service/app/runtime/rabbitmq_management.py` | **Create** | HTTP API client (peek + basic_get fallback wraps `aio_pika.Channel`) |
| `apps/agent-service/app/nodes/dlq_admin.py` | **Create** | 4 admin nodes: `dlq_inspect`, `dlq_clear_idempotent`, `dlq_dry_run`, `dlq_requeue` |
| `apps/agent-service/app/wiring/admin.py` | Modify | Register 4 DLQ admin Source.http endpoints |
| `apps/agent-service/app/runtime/outbox.py` | **Create** | `OutboxEmitter` + `transactional_emit` context manager + `RUNTIME_OUTBOX_DDL` |
| `apps/agent-service/app/runtime/outbox_dispatcher.py` | **Create** | `dispatcher_loop` (FOR UPDATE SKIP LOCKED, lane-filtered) |
| `apps/agent-service/app/runtime/dlq_audit.py` | **Create** | `RUNTIME_DLQ_AUDIT_DDL` + audit row helpers |
| `apps/agent-service/app/runtime/runtime.py` | Modify | Start `dispatcher_loop` task in `Runtime.run()` |
| `apps/agent-service/app/main.py` | Modify | Start `dispatcher_loop` task in lifespan (dual-entry) |
| `apps/agent-service/app/runtime/__init__.py` | Modify | Export `transactional_emit`, `DuplicateData`, `NeedsReview` |
| **Mutation nodes** (8 files) | Modify | Migrate `await emit(...)` from outside `async with get_session()` to inside via `transactional_emit` |
| `apps/agent-service/app/wiring/safety.py` (or wherever life_dataflow wire is) | Modify | Drop `# 不要 try/except` comment + add `.on_error("dlq")` |
| `Makefile` | Modify | Add `dlq-inspect`, `dlq-replay`, `dlq-dry-run` targets |
| `docs/runbooks/dlq-replay.md` | **Create** | Operations runbook |
| `.github/workflows/grep-gate.yml` | Modify | Close Gap 8 / 12 / 18 gates |

**Tests** (all under `apps/agent-service/tests/runtime/` unless noted):
- `test_errors.py` — exception class hierarchy
- `test_wire_on_error.py` — `WireBuilder.on_error()` validation
- `test_durable_error_routing.py` — `_route_consumer_exception` 6 paths
- `test_review_queue.py` — `publish_to_review_queue` + `mark_review`
- `test_inflight_review_skip.py` — `claim_inflight` skips `review`
- `test_delete_inflight.py` — both modes + `AlreadySucceededError`
- `test_rabbitmq_management.py` — HTTP API client
- `test_dlq_admin.py` — 4 admin endpoints (mock RabbitMQ + DB)
- `test_outbox.py` — `OutboxEmitter.append` + transactional rollback
- `test_outbox_dispatcher.py` — `dispatcher_loop` (mock `emit`, FOR UPDATE SKIP LOCKED, lane filter)
- `tests/integration/test_outbox_e2e.py` — end-to-end with real RabbitMQ + Postgres

---

## Project conventions for this plan

1. **Run tests via uv**: `uv run pytest <path> -v` (per global CLAUDE.md)
2. **DDL lists**: each new runtime table has a `RUNTIME_<NAME>_DDL: list[str]` exported from its module; bootstrapped via `runtime/migrator.py` startup hook (existing pattern, see `runtime/inflight.py:49`).
3. **Lane normalization**: always use `from app.infra.rabbitmq import current_lane` (defined at `infra/rabbitmq.py:122`). Never read `lane_var.get()` directly outside `infra/` and `runtime/propagation.py`.
4. **Commits**: Conventional Commits + `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` footer (HEREDOC), per project commit recipe.
5. **No `--no-verify`**: pre-commit hooks must pass.
6. **CI gate** (`.github/workflows/grep-gate.yml`) is touched only in Task 11.

---

# Tasks

---

## Task 1 — Gap 18: typed errors + wire `.on_error()` builder

**Goal:** Expose DSL surface without changing handler behavior. After this task, `wire(X).on_error("manual-review")` parses but durable handler still behaves as in 7a.

**Files:**
- Create: `apps/agent-service/app/runtime/errors.py`
- Modify: `apps/agent-service/app/runtime/wire.py`
- Modify: `apps/agent-service/app/runtime/__init__.py`
- Test: `apps/agent-service/tests/runtime/test_errors.py`
- Test: `apps/agent-service/tests/runtime/test_wire_on_error.py`

- [ ] **Step 1.1: Create the failing test for error classes**

Create `apps/agent-service/tests/runtime/test_errors.py`:

```python
"""Phase 7b Gap 18: typed exceptions for error policy."""
import pytest

from app.runtime.errors import AlreadySucceededError, DuplicateData, NeedsReview


def test_duplicate_data_is_exception():
    assert issubclass(DuplicateData, Exception)
    exc = DuplicateData("dup id=42")
    assert str(exc) == "dup id=42"


def test_needs_review_is_exception():
    assert issubclass(NeedsReview, Exception)
    exc = NeedsReview("needs operator approval")
    assert str(exc) == "needs operator approval"


def test_already_succeeded_error_carries_inflight_keys():
    exc = AlreadySucceededError(edge_id="EdgeA::consumer", idempotent_key="abc123")
    assert exc.edge_id == "EdgeA::consumer"
    assert exc.idempotent_key == "abc123"
    assert "EdgeA::consumer" in str(exc)
    assert "abc123" in str(exc)


def test_already_succeeded_error_is_exception():
    assert issubclass(AlreadySucceededError, Exception)
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.runtime.errors'`

- [ ] **Step 1.3: Implement runtime/errors.py**

Create `apps/agent-service/app/runtime/errors.py`:

```python
"""Phase 7b Gap 18: typed exceptions for runtime error policy.

These exceptions are part of the framework's surface for business code:
- DuplicateData / NeedsReview: business code raises these from a consumer
  to signal a specific failure semantic; the durable handler routes them
  per the wire's on_error policy (see runtime/durable.py).
- AlreadySucceededError: raised by runtime/inflight.delete_inflight when
  a caller targets an already-succeeded inflight row in edge_idempotent
  mode. Used by the DLQ requeue protocol (zombie detection).
"""
from __future__ import annotations


class DuplicateData(Exception):
    """Business code raises this from a consumer to signal that the
    incoming Data is a business-level duplicate (beyond what the
    runtime_inflight (edge_id, idempotent_key) dedup already covers).

    Framework behavior:
      - on_error="ignore-duplicate" -> ack + log warning + no DLQ + no retry
      - on_error other values       -> falls through to generic Exception
                                       path (mark_failed + decide_retry +
                                       eventually DLQ). Safe-default for
                                       misconfigured wires.
    """


class NeedsReview(Exception):
    """Business code raises this from a consumer to signal that the
    Data requires human review before any retry / dispatch decision.

    Framework behavior:
      - on_error="manual-review"   -> publish to manual-review queue + ack
      - on_error other values      -> falls through to generic Exception
                                       path. Safe-default for misconfigured
                                       wires.
    """


class AlreadySucceededError(Exception):
    """Raised by runtime/inflight.delete_inflight in edge_idempotent mode
    when the targeted (edge_id, idempotent_key) already has state='succeeded'.

    The DLQ requeue 6-step protocol catches this and treats the original
    DLQ message as a zombie (ack + audit status='zombie_acked'); see
    nodes/dlq_admin.py.
    """

    def __init__(self, *, edge_id: str, idempotent_key: str) -> None:
        super().__init__(
            f"inflight already succeeded: edge_id={edge_id!r} "
            f"idempotent_key={idempotent_key!r}"
        )
        self.edge_id = edge_id
        self.idempotent_key = idempotent_key
```

- [ ] **Step 1.4: Run test to verify it passes**

Run: `uv run pytest apps/agent-service/tests/runtime/test_errors.py -v`
Expected: 4 passed

- [ ] **Step 1.5: Create the failing test for `WireBuilder.on_error()`**

Create `apps/agent-service/tests/runtime/test_wire_on_error.py`:

```python
"""Phase 7b Gap 18: wire(...).on_error() builder."""
import pytest

from app.runtime.data import Data, Key
from app.runtime.wire import clear_wiring, wire


class _D(Data):
    id: Key[str]


def setup_function(_fn):
    clear_wiring()


def test_default_on_error_is_dlq():
    builder = wire(_D)
    assert builder._spec.on_error == "dlq"


def test_on_error_sets_policy():
    builder = wire(_D).durable().on_error("ignore-duplicate")
    assert builder._spec.on_error == "ignore-duplicate"


def test_on_error_returns_builder_for_chaining():
    builder = wire(_D).durable()
    same = builder.on_error("manual-review")
    assert same is builder


@pytest.mark.parametrize("policy", ["dlq", "ignore-duplicate", "manual-review"])
def test_on_error_accepts_valid_policies(policy):
    wire(_D).durable().on_error(policy)


def test_on_error_rejects_invalid_policy():
    with pytest.raises(ValueError, match="on_error policy must be one of"):
        wire(_D).durable().on_error("retry")  # was a v0 idea; rejected at compile time


def test_on_error_rejects_typo():
    with pytest.raises(ValueError):
        wire(_D).durable().on_error("dql")
```

- [ ] **Step 1.6: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_wire_on_error.py -v`
Expected: FAIL — `WireSpec` has no `on_error`, builder has no `.on_error()`.

- [ ] **Step 1.7: Add `on_error` field to `WireSpec` and builder method**

In `apps/agent-service/app/runtime/wire.py`, modify the `WireSpec` dataclass (around line 47) — add field after `retry`:

```python
@dataclass
class WireSpec:
    data_type: type[Data]
    consumers: list[Callable] = field(default_factory=list)
    sinks: list[SinkSpec] = field(default_factory=list)
    sources: list[SourceSpec] = field(default_factory=list)
    durable: bool = False
    as_latest: bool = False
    predicate: Callable | None = None
    debounce: dict | None = None
    debounce_key_by: Callable[[Data], str] | None = None
    with_latest: tuple[type[Data], ...] = ()
    retry: RetryPolicy | None = None
    on_error: str = "dlq"  # Gap 18: dlq | ignore-duplicate | manual-review
```

Add a constant near the top of `wire.py` (after imports):

```python
_VALID_ON_ERROR: tuple[str, ...] = ("dlq", "ignore-duplicate", "manual-review")
```

Add a method on `WireBuilder` (after `.retry()`):

```python
    def on_error(self, policy: str) -> WireBuilder:
        """Configure error policy for this wire (Gap 18).

        Valid values: 'dlq' (default — fall to DLQ),
        'ignore-duplicate' (ack DuplicateData silently),
        'manual-review' (route NeedsReview to review queue).
        retry is controlled separately by .retry(); on_error decides
        what happens AFTER retries are exhausted or for non-retryable
        errors.
        """
        if policy not in _VALID_ON_ERROR:
            raise ValueError(
                f"on_error policy must be one of {_VALID_ON_ERROR}, got {policy!r}"
            )
        self._spec.on_error = policy
        return self
```

- [ ] **Step 1.8: Run wire tests to verify they pass**

Run: `uv run pytest apps/agent-service/tests/runtime/test_wire_on_error.py -v`
Expected: 6 passed

- [ ] **Step 1.9: Re-export from runtime package**

Modify `apps/agent-service/app/runtime/__init__.py` — add to imports:

```python
from app.runtime.errors import DuplicateData, NeedsReview
```

Add to `__all__`:

```python
    "DuplicateData",
    "NeedsReview",
```

(Preserve alphabetical / logical order with existing entries.)

- [ ] **Step 1.10: Sanity check existing wire tests still pass**

Run: `uv run pytest apps/agent-service/tests/runtime/ -v`
Expected: all green (no regressions from adding the field).

- [ ] **Step 1.11: Run linter**

Run: `uv run ruff check apps/agent-service/app/runtime/errors.py apps/agent-service/app/runtime/wire.py apps/agent-service/app/runtime/__init__.py apps/agent-service/tests/runtime/test_errors.py apps/agent-service/tests/runtime/test_wire_on_error.py`
Expected: no warnings.

- [ ] **Step 1.12: Commit**

```bash
git add apps/agent-service/app/runtime/errors.py \
        apps/agent-service/app/runtime/wire.py \
        apps/agent-service/app/runtime/__init__.py \
        apps/agent-service/tests/runtime/test_errors.py \
        apps/agent-service/tests/runtime/test_wire_on_error.py

git commit -m "$(cat <<'EOF'
feat(runtime): typed errors (DuplicateData / NeedsReview) + wire .on_error() builder

Phase 7b Gap 18 step 1/4. Exposes DSL surface; durable handler still
behaves as in 7a. Builder validates policy at compile time so typos
crash loudly. AlreadySucceededError lives here too (Gap 12 Task 5
will use it).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — Gap 18: durable handler routes by error policy

**Goal:** Replace `durable.py:217-271` with single `except Exception` + `_route_consumer_exception` helper. After this task, `on_error="ignore-duplicate"` and existing `on_error="dlq"` paths work; `manual-review` exists as a code path but its target queue isn't built yet (Task 3).

**Files:**
- Modify: `apps/agent-service/app/runtime/durable.py`
- Test: `apps/agent-service/tests/runtime/test_durable_error_routing.py`

- [ ] **Step 2.1: Read current handler shape so the diff is minimal**

Run: `uv run python -c "import inspect; from app.runtime.durable import _build_handler; print(inspect.getsource(_build_handler))"` (note in plan; not for committing).

- [ ] **Step 2.2: Write failing tests for `_route_consumer_exception`**

Create `apps/agent-service/tests/runtime/test_durable_error_routing.py`:

```python
"""Phase 7b Gap 18: durable handler routes consumer exceptions per wire.on_error."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.errors import DuplicateData, NeedsReview


@dataclass
class _FakeWire:
    on_error: str = "dlq"
    retry: Any = None


# We'll import the helper after it exists; keep a thin wrapper.
async def _call_helper(*, exc, wire, **kw):
    from app.runtime.durable import _route_consumer_exception
    return await _route_consumer_exception(
        exc, wire=wire, inflight_key=("edge", "key"),
        data=object(), attempts=kw.get("attempts", 1),
    )


@pytest.mark.asyncio
async def test_duplicate_data_with_ignore_policy_marks_succeeded_and_returns():
    wire = _FakeWire(on_error="ignore-duplicate")
    with patch("app.runtime.durable.mark_succeeded", new=AsyncMock()) as ms, \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf:
        # return (no raise) means caller will ack
        await _call_helper(exc=DuplicateData("dup"), wire=wire)
        ms.assert_awaited_once()
        mf.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_data_with_dlq_policy_falls_through_and_raises():
    wire = _FakeWire(on_error="dlq")
    with patch("app.runtime.durable.mark_succeeded", new=AsyncMock()), \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf, \
         patch("app.runtime.durable.decide_retry") as dr:
        dr.return_value = type("D", (), {"action": "dlq"})()
        with pytest.raises(DuplicateData):
            await _call_helper(exc=DuplicateData("dup"), wire=wire)
        mf.assert_awaited_once()


@pytest.mark.asyncio
async def test_needs_review_with_manual_review_publishes_and_marks_review():
    wire = _FakeWire(on_error="manual-review")
    with patch("app.runtime.durable.publish_to_review_queue", new=AsyncMock(return_value=True)) as pub, \
         patch("app.runtime.durable.mark_review", new=AsyncMock()) as mr:
        await _call_helper(exc=NeedsReview("needs op"), wire=wire)
        pub.assert_awaited_once()
        mr.assert_awaited_once()


@pytest.mark.asyncio
async def test_needs_review_publish_confirm_failed_falls_through_to_dlq():
    wire = _FakeWire(on_error="manual-review")
    with patch("app.runtime.durable.publish_to_review_queue", new=AsyncMock(return_value=False)), \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf, \
         patch("app.runtime.durable.mark_review", new=AsyncMock()) as mr:
        with pytest.raises(NeedsReview):
            await _call_helper(exc=NeedsReview("needs op"), wire=wire)
        mf.assert_awaited_once()
        mr.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_retry_publish_confirmed_acks_silently():
    wire = _FakeWire(on_error="dlq")
    with patch("app.runtime.durable.mark_failed", new=AsyncMock()), \
         patch("app.runtime.durable.decide_retry") as dr, \
         patch("app.runtime.durable.publish_with_confirm", new=AsyncMock(return_value=True)):
        dr.return_value = type("D", (), {"action": "retry", "attempt": 2, "delay_ms": 100})()
        # retry envelope publish; helper returns (ack)
        await _call_helper(exc=RuntimeError("boom"), wire=wire)


@pytest.mark.asyncio
async def test_generic_retry_publish_unconfirmed_falls_through_to_dlq():
    wire = _FakeWire(on_error="dlq")
    with patch("app.runtime.durable.mark_failed", new=AsyncMock()), \
         patch("app.runtime.durable.decide_retry") as dr, \
         patch("app.runtime.durable.publish_with_confirm", new=AsyncMock(return_value=False)):
        dr.return_value = type("D", (), {"action": "retry", "attempt": 2, "delay_ms": 100})()
        original = RuntimeError("boom")
        with pytest.raises(RuntimeError) as ei:
            await _call_helper(exc=original, wire=wire)
        assert ei.value is original
```

- [ ] **Step 2.3: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_durable_error_routing.py -v`
Expected: FAIL — `_route_consumer_exception` not defined.

- [ ] **Step 2.4: Implement `_route_consumer_exception` and refactor `_build_handler`**

> **Reading order:** sub-changes A→E build up to the final code. Sections B/C introduce a temporary stub that section E removes; the final signature lives in section E. Apply A+D+E for the final state and skip B/C (or apply them sequentially if you want git-bisectable intermediate state — both are fine since this is one commit).

In `apps/agent-service/app/runtime/durable.py`:

A) Add imports near the top (group with existing app imports):

```python
from app.runtime.errors import DuplicateData, NeedsReview
```

B) Below `_build_handler` (or just above it), add the helper:

```python
async def _route_consumer_exception(
    exc: BaseException,
    *,
    wire: WireSpec,
    inflight_key: tuple[str, str],
    data: Data,
    attempts: int,
) -> None:
    """Phase 7b Gap 18: dispatch a consumer exception per wire.on_error.

    Contract:
      - return -> 'handled' path: caller's `async with message.process(...)`
        will ack on clean exit. inflight terminal state is updated here.
      - raise  -> 'dlq' path: caller's `process(requeue=False)` will nack
        and the broker will route to DLX. Always re-raises the ORIGINAL
        exception (so DLQ message body keeps the cause).

    Helper itself NEVER calls message.ack() / message.nack() — see project
    memory feedback_aio_pika_process_context_double_ack.
    """
    edge_id, idem_key = inflight_key
    last_error = str(exc)

    # 1. typed exception in matching policy
    if isinstance(exc, DuplicateData) and wire.on_error == "ignore-duplicate":
        logger.warning(
            "durable consumer: duplicate ignored (edge=%s key=%s reason=%s)",
            edge_id, idem_key, last_error,
        )
        await mark_succeeded(edge_id=edge_id, idempotent_key=idem_key)
        return

    if isinstance(exc, NeedsReview) and wire.on_error == "manual-review":
        confirmed = await publish_to_review_queue(
            wire=wire, data=data, exc=exc,
            attempts=attempts, last_error=last_error,
        )
        if not confirmed:
            logger.warning(
                "durable consumer: review queue publish-confirm failed, "
                "falling through to DLQ (edge=%s key=%s)",
                edge_id, idem_key,
            )
            await mark_failed(edge_id=edge_id, idempotent_key=idem_key,
                              last_error=last_error)
            raise exc
        await mark_review(edge_id=edge_id, idempotent_key=idem_key)
        return

    # 2. generic Exception path (incl. typed exceptions in mismatched policies)
    await mark_failed(edge_id=edge_id, idempotent_key=idem_key,
                      last_error=last_error)
    decision = decide_retry(
        headers={},  # caller-supplied below; durable handler will pass headers
        policy=wire.retry,
    )
    # NOTE: actual handler integration in step 2.5 passes real headers.
    if decision.action == "retry":
        new_headers = {DELIVERY_COUNT_HEADER: decision.attempt}
        body = data.model_dump(mode="json")
        route = _route_for(wire, _consumer_for(wire))  # consumer resolved by caller
        confirmed = await mq.publish_with_confirm(
            route, body, headers=new_headers,
            lane=current_lane(), delay_ms=decision.delay_ms,
        )
        if not confirmed:
            logger.warning(
                "durable consumer: retry publish-confirm failed, falling "
                "through to DLQ (edge=%s key=%s attempt=%d)",
                edge_id, idem_key, decision.attempt,
            )
            raise exc
        return

    # 3. dlq fallback (.on_error("manual-review") with retry-exhausted handled here too)
    if wire.on_error == "manual-review":
        confirmed = await publish_to_review_queue(
            wire=wire, data=data, exc=exc,
            attempts=attempts, last_error=last_error,
        )
        if not confirmed:
            logger.warning(
                "durable consumer: review queue publish-confirm failed, "
                "falling through to DLQ (edge=%s key=%s)",
                edge_id, idem_key,
            )
            raise exc
        await mark_review(edge_id=edge_id, idempotent_key=idem_key)
        return

    raise exc  # default on_error="dlq" -> caller's process(requeue=False)
```

> **Important:** the helper above references `publish_to_review_queue` / `mark_review` / `_consumer_for(wire)` — these will be added in Tasks 3. To keep this commit shippable in isolation, gate them behind import-time stubs:

C) Add temporary stubs at module bottom (will be replaced in Task 3):

```python
# Phase 7b temporary stubs — replaced in Task 3 when manual-review queue lands.
async def publish_to_review_queue(*args, **kwargs):  # pragma: no cover
    raise NotImplementedError("manual-review queue not yet wired (Task 3)")


async def mark_review(*args, **kwargs):  # pragma: no cover
    raise NotImplementedError("manual-review marking not yet wired (Task 3)")


def _consumer_for(wire: WireSpec) -> Callable:
    # In real handlers, the consumer is bound at handler-build time; this
    # stub helps tests that monkeypatch the helper. Real handler in
    # _build_handler passes consumer explicitly via closure.
    if wire.consumers:
        return wire.consumers[0]
    raise RuntimeError("no consumer for wire (testing stub)")
```

D) Refactor `_build_handler` body (the current try/except at lines ~215-271). Replace the whole `try/except Exception as exc:` block (after `await consumer(**{param_name: obj})`) with:

```python
                try:
                    await consumer(**{param_name: obj})
                except Exception as exc:
                    await _route_consumer_exception(
                        exc, wire=w, inflight_key=(edge_id, idem_key),
                        data=obj, attempts=outcome.attempts,
                    )
                    # handled -> fall through to mark_succeeded
                    # dlq     -> raised; caller's process(__aexit__) acks/nacks
                await mark_succeeded(
                    edge_id=edge_id,
                    idempotent_key=idem_key,
                )
```

> Keep the inner `if confirmed:` retry logic that currently lives in lines 248-264 if your editor preserves it; the new `_route_consumer_exception` subsumes that path, so DELETE the old retry/decide_retry block (lines ~217-271 in the v3+ tree). Confirm before continuing: `grep -n "decide_retry" apps/agent-service/app/runtime/durable.py` — only one occurrence (inside `_route_consumer_exception`).

E) Adjust the helper to receive `consumer` directly (cleaner than the stub):

In `_build_handler`, capture `consumer` and pass it to the helper. Update helper signature:

```python
async def _route_consumer_exception(
    exc: BaseException,
    *,
    wire: WireSpec,
    consumer: Callable,         # NEW
    inflight_key: tuple[str, str],
    data: Data,
    attempts: int,
    headers: dict | None = None,  # NEW — caller passes message.headers
) -> None:
```

Inside the helper, replace `_route_for(wire, _consumer_for(wire))` with `_route_for(wire, consumer)`, and `decide_retry(headers={}, ...)` with `decide_retry(headers=headers or {}, policy=wire.retry)`. Drop the `_consumer_for` stub.

Update tests' `_call_helper` to pass `consumer`:

```python
async def _call_helper(*, exc, wire, **kw):
    from app.runtime.durable import _route_consumer_exception
    async def _fake(): pass
    return await _route_consumer_exception(
        exc, wire=wire, consumer=_fake,
        inflight_key=("edge", "key"),
        data=object(), attempts=kw.get("attempts", 1),
        headers={},
    )
```

- [ ] **Step 2.5: Run targeted tests**

Run: `uv run pytest apps/agent-service/tests/runtime/test_durable_error_routing.py -v`
Expected: 6 passed.

- [ ] **Step 2.6: Run full durable test suite to verify no regression**

Run: `uv run pytest apps/agent-service/tests/runtime/test_durable.py apps/agent-service/tests/runtime/test_durable_retry.py -v`
Expected: all green. (7a retry semantics preserved by `decide_retry` + retry-confirm-failed fall-through.)

- [ ] **Step 2.7: Run linter**

Run: `uv run ruff check apps/agent-service/app/runtime/durable.py apps/agent-service/tests/runtime/test_durable_error_routing.py`
Expected: no warnings.

- [ ] **Step 2.8: Commit**

```bash
git add apps/agent-service/app/runtime/durable.py \
        apps/agent-service/tests/runtime/test_durable_error_routing.py
git commit -m "$(cat <<'EOF'
feat(runtime): durable handler routes by error policy (single-except + helper)

Phase 7b Gap 18 step 2/4. Replaces ad-hoc try/except in _build_handler
with _route_consumer_exception following the explicit handled-return /
dlq-raise contract. publish-confirm failure on retry envelope (or, in
Task 3, on review-queue publish) falls through to DLQ raise so messages
are never silently dropped. mark_review / publish_to_review_queue are
NotImplementedError stubs until Task 3.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — Gap 18: manual-review queue + `claim_inflight` review skip + `mark_review`

**Goal:** Wire up `publish_to_review_queue` / `mark_review` and teach `claim_inflight` to treat `state='review'` as a skip-terminal. After this task, `wire(...).on_error("manual-review")` is fully functional.

**Files:**
- Create: `apps/agent-service/app/runtime/review_queue.py`
- Modify: `apps/agent-service/app/runtime/inflight.py`
- Modify: `apps/agent-service/app/runtime/durable.py` (delete the temporary stubs from Task 2)
- Modify: `apps/agent-service/app/infra/rabbitmq.py` (declare review queue per durable wire)
- Test: `apps/agent-service/tests/runtime/test_review_queue.py`
- Test: `apps/agent-service/tests/runtime/test_inflight_review_skip.py`

- [ ] **Step 3.1: Failing test — claim_inflight skips review**

Create `apps/agent-service/tests/runtime/test_inflight_review_skip.py`:

```python
"""Phase 7b Gap 18 round-4 finding 1: claim_inflight skips review terminal."""
import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.inflight import (
    RUNTIME_INFLIGHT_DDL,
    claim_inflight,
    mark_review,
)


@pytest.fixture(autouse=True)
async def _setup():
    async with get_session() as s:
        for ddl in RUNTIME_INFLIGHT_DDL:
            await s.execute(text(ddl))
        await s.execute(text("DELETE FROM runtime_inflight"))
        await s.commit()


@pytest.mark.asyncio
async def test_claim_skips_succeeded():
    edge, key = "edgeA::cons", "k1"
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) VALUES (:e, :k, 't', 'succeeded', 1)"
        ), {"e": edge, "k": key})
        await s.commit()
    out = await claim_inflight(
        edge_id=edge, idempotent_key=key, data_table="t",
        worker_id="w", lease_ms=1000,
    )
    assert out.action == "skip"


@pytest.mark.asyncio
async def test_claim_skips_review():
    edge, key = "edgeA::cons", "k2"
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) VALUES (:e, :k, 't', 'review', 1)"
        ), {"e": edge, "k": key})
        await s.commit()
    out = await claim_inflight(
        edge_id=edge, idempotent_key=key, data_table="t",
        worker_id="w", lease_ms=1000,
    )
    assert out.action == "skip"


@pytest.mark.asyncio
async def test_mark_review_writes_review_state():
    edge, key = "edgeA::cons", "k3"
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) VALUES (:e, :k, 't', 'processing', 1)"
        ), {"e": edge, "k": key})
        await s.commit()
    await mark_review(edge_id=edge, idempotent_key=key)
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT state FROM runtime_inflight WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge, "k": key})).mappings().first()
    assert row["state"] == "review"
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_inflight_review_skip.py -v`
Expected: FAIL — `mark_review` not defined; `claim_inflight` doesn't skip `review`.

- [ ] **Step 3.3: Modify `claim_inflight` and add `mark_review`**

In `apps/agent-service/app/runtime/inflight.py`:

A) Change the skip-state check at line 137:

```python
        # Phase 7b Gap 18 round-4 finding 1: review is a terminal too.
        if state in ("succeeded", "review"):
            return ClaimOutcome(action="skip", attempts=0, fresh=False)
```

B) Add `mark_review` after `mark_failed` (mirror its shape):

```python
async def mark_review(*, edge_id: str, idempotent_key: str) -> None:
    """Phase 7b Gap 18: terminal state for messages routed to manual-review.

    Once a row is in 'review', claim_inflight will skip it. Operators
    must delete_inflight() it before any replay (see runbook).
    """
    async with get_session() as s:
        await s.execute(text(
            "UPDATE runtime_inflight "
            "SET state='review', locked_until=NULL, worker_id=NULL, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge_id, "k": idempotent_key})
        await s.commit()
```

- [ ] **Step 3.4: Verify inflight test passes**

Run: `uv run pytest apps/agent-service/tests/runtime/test_inflight_review_skip.py -v`
Expected: 3 passed.

- [ ] **Step 3.5: Failing test — review queue declaration + publish**

Create `apps/agent-service/tests/runtime/test_review_queue.py`:

```python
"""Phase 7b Gap 18: per-wire manual-review queue + publish_to_review_queue."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.data import Data, Key
from app.runtime.review_queue import (
    review_queue_name_for,
    publish_to_review_queue,
)
from app.runtime.wire import WireSpec


class _D(Data):
    id: Key[str]


def _consumer(): pass


def test_queue_name_is_per_data_per_consumer():
    spec = WireSpec(data_type=_D, consumers=[_consumer], durable=True,
                    on_error="manual-review")
    name = review_queue_name_for(spec, _consumer)
    assert "_review" in name
    assert "_d" in name.lower()
    assert "_consumer" in name


@pytest.mark.asyncio
async def test_publish_returns_true_on_confirmed():
    spec = WireSpec(data_type=_D, consumers=[_consumer], durable=True,
                    on_error="manual-review")
    with patch("app.runtime.review_queue.mq") as mq_mock:
        mq_mock.publish_with_confirm = AsyncMock(return_value=True)
        ok = await publish_to_review_queue(
            wire=spec, consumer=_consumer, data=_D(id="x"),
            exc=RuntimeError("boom"), attempts=2, last_error="boom",
        )
        assert ok is True
        mq_mock.publish_with_confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_returns_false_when_unconfirmed():
    spec = WireSpec(data_type=_D, consumers=[_consumer], durable=True,
                    on_error="manual-review")
    with patch("app.runtime.review_queue.mq") as mq_mock:
        mq_mock.publish_with_confirm = AsyncMock(return_value=False)
        ok = await publish_to_review_queue(
            wire=spec, consumer=_consumer, data=_D(id="x"),
            exc=RuntimeError("boom"), attempts=2, last_error="boom",
        )
        assert ok is False
```

- [ ] **Step 3.6: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_review_queue.py -v`
Expected: FAIL — module `review_queue` missing.

- [ ] **Step 3.7: Implement `runtime/review_queue.py`**

Create `apps/agent-service/app/runtime/review_queue.py`:

```python
"""Phase 7b Gap 18: per-wire manual-review queue.

Each durable wire with on_error='manual-review' gets its own queue
named `durable_<data_snake>_<consumer>_review`. Unlike DLQ, the review
queue is a TERMINAL — it has no DLX and no consumer. Operators inspect
via /admin/dlq/inspect (queue_kind='review') and decide manually
(replay → delete_inflight + re-publish to original durable queue;
ignore → ack via /admin/dlq/requeue with no reroute).

publish_to_review_queue uses mq.publish_with_confirm so the durable
handler can fall through to DLQ when the broker doesn't confirm.
"""
from __future__ import annotations

import logging
from typing import Callable

from app.infra.rabbitmq import current_lane, mq
from app.runtime.data import Data
from app.runtime.naming import camel_to_snake
from app.runtime.propagation import inject_context
from app.runtime.wire import WireSpec

logger = logging.getLogger(__name__)


def review_queue_name_for(wire: WireSpec, consumer: Callable) -> str:
    """Per-(data, consumer) review queue name."""
    data_snake = camel_to_snake(wire.data_type.__name__)
    return f"durable_{data_snake}_{consumer.__name__}_review"


async def publish_to_review_queue(
    *,
    wire: WireSpec,
    consumer: Callable,
    data: Data,
    exc: BaseException,
    attempts: int,
    last_error: str,
) -> bool:
    """Publish a NeedsReview-tagged envelope to the wire's review queue.

    Returns True iff broker confirmed; on False the durable handler
    falls through to DLQ raise (helper contract).
    """
    queue = review_queue_name_for(wire, consumer)
    body = {
        "data": data.model_dump(mode="json"),
        "data_type": f"{type(data).__module__}.{type(data).__qualname__}",
        "exc_class": type(exc).__name__,
        "last_error": last_error,
        "attempts": attempts,
    }
    headers = inject_context({"data_type": "manual_review_envelope"})
    confirmed = await mq.publish_with_confirm(
        # Routes for review queues are declared at startup; we publish via
        # default exchange + routing-key=queue-name (direct binding).
        route=_route_for_review(queue),
        body=body,
        headers=headers,
        lane=current_lane(),
    )
    if not confirmed:
        logger.warning(
            "review queue publish-confirm FAILED queue=%s data_type=%s",
            queue, body["data_type"],
        )
    return confirmed


def _route_for_review(queue: str):
    # Review queues are simple direct-bound queues; use the runtime's
    # existing Route/ALL_ROUTES registry so mq.publish handles lane
    # suffix uniformly with other durable queues.
    from app.infra.rabbitmq import ALL_ROUTES, Route
    if queue in ALL_ROUTES:
        return ALL_ROUTES[queue]
    # Auto-register on first publish; lane-suffix happens inside mq.publish.
    route = Route(queue=queue, routing_key=queue)
    ALL_ROUTES[queue] = route
    return route
```

> **Naming check:** verify `app.runtime.naming.camel_to_snake` exists. If not, replace with the helper used in `runtime/durable.py` for queue naming (`grep -n "camel_to_snake\|snake" apps/agent-service/app/runtime/naming.py`). If neither exists, copy the inline conversion from `runtime/durable.py:74-77` into `naming.py` first.

- [ ] **Step 3.8: Wire compile_graph to declare review queues at startup**

Find where `compile_graph` builds the route table (`apps/agent-service/app/runtime/graph.py` — search for `ALL_ROUTES` or `route_for`). Add post-pass:

```python
    # Phase 7b Gap 18: register review queue per durable wire that opted
    # into on_error='manual-review'. Idempotent — _route_for_review checks
    # ALL_ROUTES.
    for w in WIRING_REGISTRY:
        if not w.durable or w.on_error != "manual-review":
            continue
        from app.runtime.review_queue import _route_for_review, review_queue_name_for
        for c in w.consumers:
            _route_for_review(review_queue_name_for(w, c))
```

- [ ] **Step 3.9: Replace the temporary stubs in durable.py**

In `apps/agent-service/app/runtime/durable.py`:

A) Delete the `# Phase 7b temporary stubs` block from Task 2.
B) Add proper imports at the top:

```python
from app.runtime.inflight import mark_review
from app.runtime.review_queue import publish_to_review_queue
```

C) Update the helper's `publish_to_review_queue(...)` calls to pass `consumer=consumer`:

```python
        confirmed = await publish_to_review_queue(
            wire=wire, consumer=consumer, data=data, exc=exc,
            attempts=attempts, last_error=last_error,
        )
```

- [ ] **Step 3.10: Re-run all touched tests**

Run:
```bash
uv run pytest apps/agent-service/tests/runtime/test_review_queue.py \
              apps/agent-service/tests/runtime/test_inflight_review_skip.py \
              apps/agent-service/tests/runtime/test_durable_error_routing.py -v
```
Expected: all green (durable routing tests now exercise real `publish_to_review_queue` instead of mocks of stubs — adjust patch targets if tests reference the stub paths; patches in Task 2 already use `app.runtime.durable.publish_to_review_queue` which after Step 3.9 is the imported name, so unittest.mock will patch the import in the durable module — confirm).

- [ ] **Step 3.11: Run full durable + inflight suites**

Run: `uv run pytest apps/agent-service/tests/runtime/test_durable.py apps/agent-service/tests/runtime/test_durable_retry.py apps/agent-service/tests/runtime/test_inflight*.py -v`
Expected: all green.

- [ ] **Step 3.12: Lint**

Run: `uv run ruff check apps/agent-service/app/runtime/review_queue.py apps/agent-service/app/runtime/inflight.py apps/agent-service/app/runtime/durable.py apps/agent-service/app/runtime/graph.py apps/agent-service/tests/runtime/test_review_queue.py apps/agent-service/tests/runtime/test_inflight_review_skip.py`
Expected: no warnings.

- [ ] **Step 3.13: Commit**

```bash
git add apps/agent-service/app/runtime/review_queue.py \
        apps/agent-service/app/runtime/inflight.py \
        apps/agent-service/app/runtime/durable.py \
        apps/agent-service/app/runtime/graph.py \
        apps/agent-service/tests/runtime/test_review_queue.py \
        apps/agent-service/tests/runtime/test_inflight_review_skip.py
git commit -m "$(cat <<'EOF'
feat(runtime): manual-review queue + publish_to_review_queue + claim_inflight skip review

Phase 7b Gap 18 step 3/4. Per-wire review queue is declared at
compile_graph startup for any durable wire with on_error='manual-review'.
mark_review writes inflight terminal state; claim_inflight extends its
skip-terminal set to {succeeded, review} so a replayed message in the
durable queue does NOT take over a review row and double-publish into
the review queue. Replaces the temporary stubs from the previous commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4 — Gap 18: refactor business code (drop "不要 catch" + explicit `.on_error("dlq")`)

**Goal:** Close the only known business-side leak (`nodes/life_dataflow.py:305-306` comment) and surface the (now-default) DLQ policy at the wire declaration site.

**Files:**
- Modify: `apps/agent-service/app/nodes/life_dataflow.py`
- Modify: the wiring file declaring the wire that points at the affected node — find via grep

- [ ] **Step 4.1: Locate the affected wire declaration**

Run: `grep -rn "life_dataflow\|to(\(.*life" apps/agent-service/app/wiring/ apps/agent-service/app/runtime/ | head`
Goal: identify the file declaring `wire(<TriggerData>).to(<life_dataflow node>)` to attach `.on_error("dlq")`. Confirm by reading the wire definition.

- [ ] **Step 4.2: Drop the comment in `nodes/life_dataflow.py`**

In `apps/agent-service/app/nodes/life_dataflow.py`, delete the `# 不要 try/except` (or English equivalent) comment block at lines 305-306. The code below it does not change.

- [ ] **Step 4.3: Add `.on_error("dlq")` to the wire**

In the wiring file from Step 4.1, append `.on_error("dlq")` to the relevant `.durable()` chain. Example:

```python
wire(SomeTrigger).to(node_in_life_dataflow).durable().on_error("dlq")
```

This is a no-op at runtime (the default is already `"dlq"`), but it makes the policy explicit at the declaration site so future authors see it.

- [ ] **Step 4.4: Verify the grep gate is now zero for the comment**

Run: `grep -rn "# 不要 catch\|# 不要 try/except\|# don't catch" apps/agent-service/app/{nodes,agent,chat,life,memory}/ ; echo "exit=$?"`
Expected: no matches; `exit=1`.

- [ ] **Step 4.5: Full test suite**

Run: `uv run pytest apps/agent-service/tests/runtime/ apps/agent-service/tests/dataflow/ -v`
Expected: all green.

- [ ] **Step 4.6: Lint**

Run: `uv run ruff check apps/agent-service/app/nodes/life_dataflow.py <wiring file from step 4.1>`

- [ ] **Step 4.7: Commit**

```bash
git add apps/agent-service/app/nodes/life_dataflow.py <wiring-file>
git commit -m "$(cat <<'EOF'
refactor(nodes): drop "不要 catch" comment + explicit .on_error("dlq")

Phase 7b Gap 18 step 4/4. The runtime now expresses DLQ-as-fallthrough
via wire(...).on_error('dlq'); the in-code comment that warned
authors not to wrap consumers in try/except is no longer load-bearing.
Closes Gap 18 business-side leak.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5 — Gap 12: `delete_inflight` (per-mode) + RabbitMQ Management API client

**Goal:** Add the inflight delete primitive (with the per-mode semantics decided in spec round-4) and the HTTP client used by `/admin/dlq/inspect`. Sets up the building blocks for Task 6's admin endpoints.

**Files:**
- Modify: `apps/agent-service/app/runtime/inflight.py` (add `DeleteOutcome` + `delete_inflight`)
- Create: `apps/agent-service/app/runtime/rabbitmq_management.py`
- Test: `apps/agent-service/tests/runtime/test_delete_inflight.py`
- Test: `apps/agent-service/tests/runtime/test_rabbitmq_management.py`

- [ ] **Step 5.1: Failing test for `delete_inflight`**

Create `apps/agent-service/tests/runtime/test_delete_inflight.py`:

```python
"""Phase 7b Gap 12: delete_inflight per-mode semantics."""
import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.errors import AlreadySucceededError
from app.runtime.inflight import RUNTIME_INFLIGHT_DDL, delete_inflight


@pytest.fixture(autouse=True)
async def _setup():
    async with get_session() as s:
        for ddl in RUNTIME_INFLIGHT_DDL:
            await s.execute(text(ddl))
        await s.execute(text("DELETE FROM runtime_inflight"))
        await s.commit()


async def _insert(state: str, *, edge="e", key="k", trace="t"):
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts, trace_id) "
            "VALUES (:e, :k, 'tbl', :s, 1, :t)"
        ), {"e": edge, "k": key, "s": state, "t": trace})
        await s.commit()


@pytest.mark.asyncio
async def test_edge_idempotent_deletes_failed_row():
    await _insert("failed", edge="e1", key="k1")
    out = await delete_inflight(by="edge_idempotent", edge_id="e1", idempotent_key="k1")
    assert out.deleted == 1
    assert out.skipped_succeeded == 0


@pytest.mark.asyncio
async def test_edge_idempotent_deletes_review_row():
    await _insert("review", edge="e1", key="k2")
    out = await delete_inflight(by="edge_idempotent", edge_id="e1", idempotent_key="k2")
    assert out.deleted == 1


@pytest.mark.asyncio
async def test_edge_idempotent_raises_on_succeeded():
    await _insert("succeeded", edge="e2", key="k3")
    with pytest.raises(AlreadySucceededError) as ei:
        await delete_inflight(by="edge_idempotent", edge_id="e2", idempotent_key="k3")
    assert ei.value.edge_id == "e2"
    assert ei.value.idempotent_key == "k3"


@pytest.mark.asyncio
async def test_edge_idempotent_no_row_returns_zero():
    out = await delete_inflight(by="edge_idempotent", edge_id="missing", idempotent_key="x")
    assert out.deleted == 0


@pytest.mark.asyncio
async def test_trace_id_deletes_only_non_succeeded():
    await _insert("failed", edge="e1", key="k1", trace="trace-a")
    await _insert("processing", edge="e2", key="k2", trace="trace-a")
    await _insert("succeeded", edge="e3", key="k3", trace="trace-a")
    await _insert("review", edge="e4", key="k4", trace="trace-a")
    out = await delete_inflight(by="trace_id", trace_id="trace-a")
    assert out.deleted == 3
    assert out.skipped_succeeded == 1
    # confirm the succeeded row still exists
    async with get_session() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM runtime_inflight WHERE trace_id='trace-a'"
        ))).scalar()
    assert n == 1


@pytest.mark.asyncio
async def test_trace_id_does_not_raise_on_succeeded_present():
    await _insert("succeeded", edge="e1", key="k1", trace="trace-b")
    # No raise — just skipped count
    out = await delete_inflight(by="trace_id", trace_id="trace-b")
    assert out.deleted == 0
    assert out.skipped_succeeded == 1


def test_delete_inflight_rejects_unknown_mode():
    import asyncio
    with pytest.raises(ValueError, match="by must be one of"):
        asyncio.run(delete_inflight(by="banana"))


def test_delete_inflight_rejects_missing_args():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(delete_inflight(by="trace_id"))  # trace_id=None
    with pytest.raises(ValueError):
        asyncio.run(delete_inflight(by="edge_idempotent", edge_id="e"))  # idempotent_key=None
```

- [ ] **Step 5.2: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_delete_inflight.py -v`
Expected: FAIL — `delete_inflight` not defined.

- [ ] **Step 5.3: Implement `delete_inflight` + `DeleteOutcome`**

In `apps/agent-service/app/runtime/inflight.py`:

A) Below `ClaimOutcome`:

```python
@dataclass(frozen=True)
class DeleteOutcome:
    deleted: int
    skipped_succeeded: int
```

B) Add the function (place near `mark_review`):

```python
async def delete_inflight(
    *,
    by: Literal["edge_idempotent", "trace_id"],
    trace_id: str | None = None,
    edge_id: str | None = None,
    idempotent_key: str | None = None,
) -> DeleteOutcome:
    """Phase 7b Gap 12: clear inflight rows for DLQ replay.

    Modes:
      - edge_idempotent: target a single (edge_id, idempotent_key) row.
        Refuses to delete a 'succeeded' row — raises AlreadySucceededError
        so the DLQ requeue protocol can route to the zombie-ack path.
      - trace_id: delete every non-succeeded row for the trace; preserves
        succeeded rows (mixed-state traces are normal). Returns counts;
        never raises AlreadySucceededError.
    """
    if by not in ("edge_idempotent", "trace_id"):
        raise ValueError(
            f"by must be one of ('edge_idempotent', 'trace_id'), got {by!r}"
        )

    if by == "edge_idempotent":
        if not edge_id or not idempotent_key:
            raise ValueError(
                "edge_idempotent mode requires both edge_id and idempotent_key"
            )
        async with get_session() as s:
            row = (await s.execute(text(
                "SELECT state FROM runtime_inflight "
                "WHERE edge_id=:e AND idempotent_key=:k"
            ), {"e": edge_id, "k": idempotent_key})).mappings().first()
            if row is None:
                return DeleteOutcome(deleted=0, skipped_succeeded=0)
            if row["state"] == "succeeded":
                raise AlreadySucceededError(
                    edge_id=edge_id, idempotent_key=idempotent_key,
                )
            await s.execute(text(
                "DELETE FROM runtime_inflight "
                "WHERE edge_id=:e AND idempotent_key=:k AND state != 'succeeded'"
            ), {"e": edge_id, "k": idempotent_key})
            await s.commit()
        return DeleteOutcome(deleted=1, skipped_succeeded=0)

    # by == "trace_id"
    if not trace_id:
        raise ValueError("trace_id mode requires trace_id")
    async with get_session() as s:
        skipped = (await s.execute(text(
            "SELECT count(*) FROM runtime_inflight "
            "WHERE trace_id=:t AND state='succeeded'"
        ), {"t": trace_id})).scalar()
        result = await s.execute(text(
            "DELETE FROM runtime_inflight "
            "WHERE trace_id=:t AND state != 'succeeded'"
        ), {"t": trace_id})
        await s.commit()
        deleted = result.rowcount or 0
    return DeleteOutcome(deleted=deleted, skipped_succeeded=int(skipped or 0))
```

C) Add the import (top of file):

```python
from app.runtime.errors import AlreadySucceededError
```

D) Add `Literal` to existing typing import:

```python
from typing import Literal
```

- [ ] **Step 5.4: Verify test passes**

Run: `uv run pytest apps/agent-service/tests/runtime/test_delete_inflight.py -v`
Expected: 8 passed.

- [ ] **Step 5.5: Failing test for RabbitMQ management client**

Create `apps/agent-service/tests/runtime/test_rabbitmq_management.py`:

```python
"""Phase 7b Gap 12: RabbitMQ management HTTP API client."""
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.rabbitmq_management import RabbitMQManagementClient


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RABBITMQ_HOST", "rabbit-host")
    monkeypatch.setenv("RABBITMQ_USER", "user")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "secret")
    monkeypatch.setenv("RABBITMQ_MANAGEMENT_PORT", "15672")
    monkeypatch.setenv("RABBITMQ_VHOST", "/")


@pytest.mark.asyncio
async def test_peek_messages_calls_get_endpoint(env):
    client = RabbitMQManagementClient.from_env()
    fake_resp = [{"properties": {}, "payload": "{}", "redelivered": False}]
    with patch.object(client, "_post_json", new=AsyncMock(return_value=fake_resp)) as p:
        rows = await client.peek_messages(queue="some_dlq", limit=5)
        assert rows == fake_resp
        called_url, body = p.call_args[0]
        assert called_url.endswith("/api/queues/%2F/some_dlq/get")
        assert body["count"] == 5
        assert body["ackmode"] == "ack_requeue_true"  # peek mode


@pytest.mark.asyncio
async def test_management_uses_basic_auth(env):
    client = RabbitMQManagementClient.from_env()
    assert client.auth == ("user", "secret")
    assert client.base_url == "http://rabbit-host:15672"


@pytest.mark.asyncio
async def test_vhost_url_encoded(env, monkeypatch):
    monkeypatch.setenv("RABBITMQ_VHOST", "my-vhost")
    client = RabbitMQManagementClient.from_env()
    with patch.object(client, "_post_json", new=AsyncMock(return_value=[])):
        await client.peek_messages(queue="q", limit=1)
        url = client._post_json.call_args[0][0]
        assert "/api/queues/my-vhost/q/get" in url
```

- [ ] **Step 5.6: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_rabbitmq_management.py -v`
Expected: FAIL — module missing.

- [ ] **Step 5.7: Implement client**

Create `apps/agent-service/app/runtime/rabbitmq_management.py`:

```python
"""Phase 7b Gap 12: minimal RabbitMQ Management HTTP API client.

Used by /admin/dlq/inspect to peek at DLQ / review-queue contents
without consuming. Credentials piggyback on the AMQP user (see
ConfigBundle conventions). For requeue, use the AMQP basic_get path
in nodes/dlq_admin (the management API has no transactional guarantees).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


@dataclass
class RabbitMQManagementClient:
    base_url: str
    auth: tuple[str, str]
    vhost: str

    @classmethod
    def from_env(cls) -> RabbitMQManagementClient:
        host = os.environ["RABBITMQ_HOST"]
        port = os.getenv("RABBITMQ_MANAGEMENT_PORT", "15672")
        user = os.environ["RABBITMQ_USER"]
        pw = os.environ["RABBITMQ_PASSWORD"]
        vhost = os.getenv("RABBITMQ_VHOST", "/")
        return cls(
            base_url=f"http://{host}:{port}",
            auth=(user, pw),
            vhost=vhost,
        )

    async def peek_messages(
        self, *, queue: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List up to ``limit`` messages without consuming.

        Uses ackmode=ack_requeue_true so messages stay in the queue
        (the management API requeues them after the read).
        """
        vhost_enc = quote(self.vhost, safe="")
        url = f"{self.base_url}/api/queues/{vhost_enc}/{queue}/get"
        body = {
            "count": limit,
            "ackmode": "ack_requeue_true",
            "encoding": "auto",
            "truncate": 50000,
        }
        return await self._post_json(url, body)

    async def _post_json(self, url: str, body: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(auth=self.auth, timeout=10.0) as c:
            r = await c.post(url, json=body)
            r.raise_for_status()
            return r.json()
```

- [ ] **Step 5.8: Verify management test passes**

Run: `uv run pytest apps/agent-service/tests/runtime/test_rabbitmq_management.py -v`
Expected: 3 passed.

- [ ] **Step 5.9: Lint**

Run: `uv run ruff check apps/agent-service/app/runtime/inflight.py apps/agent-service/app/runtime/rabbitmq_management.py apps/agent-service/tests/runtime/test_delete_inflight.py apps/agent-service/tests/runtime/test_rabbitmq_management.py`

- [ ] **Step 5.10: Commit**

```bash
git add apps/agent-service/app/runtime/inflight.py \
        apps/agent-service/app/runtime/rabbitmq_management.py \
        apps/agent-service/tests/runtime/test_delete_inflight.py \
        apps/agent-service/tests/runtime/test_rabbitmq_management.py
git commit -m "$(cat <<'EOF'
feat(runtime): inflight.delete_inflight() + RabbitMQ management client

Phase 7b Gap 12 step 1/3. delete_inflight has per-mode semantics:
edge_idempotent raises AlreadySucceededError on succeeded targets
(zombie path); trace_id deletes only non-succeeded rows and reports
skipped_succeeded count, never raises. Management client wraps the
HTTP /api/queues/{vhost}/{q}/get endpoint for peek-mode DLQ inspect.
Credentials (RABBITMQ_USER / PASSWORD / MANAGEMENT_PORT) piggyback
on the AMQP account per ConfigBundle convention.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6 — Gap 12: admin DLQ endpoints + audit table + 6-step requeue

**Goal:** Implement the four admin nodes, the audit table, and the transaction-like requeue protocol. After this task, `/admin/dlq/{inspect,clear-idempotent,dry-run,requeue}` are reachable end-to-end.

**Files:**
- Create: `apps/agent-service/app/runtime/dlq_audit.py` (DDL + audit row helpers)
- Create: `apps/agent-service/app/nodes/dlq_admin.py` (4 admin nodes)
- Modify: `apps/agent-service/app/wiring/admin.py` (register Source.http endpoints)
- Modify: `apps/agent-service/app/runtime/migrator.py` (or wherever boot DDL is applied) — register `RUNTIME_DLQ_AUDIT_DDL`
- Test: `apps/agent-service/tests/runtime/test_dlq_audit.py`
- Test: `apps/agent-service/tests/runtime/test_dlq_admin.py`

- [ ] **Step 6.1: Failing test — audit DDL applies and helpers work**

Create `apps/agent-service/tests/runtime/test_dlq_audit.py`:

```python
"""Phase 7b Gap 12: runtime_dlq_audit DDL + helpers."""
import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.dlq_audit import (
    RUNTIME_DLQ_AUDIT_DDL,
    AuditAction,
    AuditStatus,
    insert_audit_row,
    update_audit_status,
)


@pytest.fixture(autouse=True)
async def _setup():
    async with get_session() as s:
        for ddl in RUNTIME_DLQ_AUDIT_DDL:
            await s.execute(text(ddl))
        await s.execute(text("DELETE FROM runtime_dlq_audit"))
        await s.commit()


@pytest.mark.asyncio
async def test_insert_and_update_status_round_trip():
    audit_id = await insert_audit_row(
        action=AuditAction.REQUEUE, status=AuditStatus.CLEARED,
        queue="durable_some_dlx", queue_kind="dlq",
        message_ids=["m1"], recovery_token="m1",
        recovery_hint=None, cleared_inflight_count=1,
        requeued_count=0, operator="alice", trace_id="t-x",
    )
    assert audit_id > 0
    await update_audit_status(audit_id, AuditStatus.REQUEUED, requeued_count=1)
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT status, requeued_count FROM runtime_dlq_audit WHERE id=:i"
        ), {"i": audit_id})).mappings().first()
    assert row["status"] == "requeued"
    assert row["requeued_count"] == 1
```

- [ ] **Step 6.2: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_dlq_audit.py -v`
Expected: FAIL.

- [ ] **Step 6.3: Implement `runtime/dlq_audit.py`**

Create `apps/agent-service/app/runtime/dlq_audit.py`:

```python
"""Phase 7b Gap 12: runtime_dlq_audit DDL + helpers.

Status state machine (see spec §3.2 6-step protocol):
  cleared -> requeued | publish_failed | zombie_acked | already_succeeded
"""
from __future__ import annotations

import json
from enum import StrEnum

from sqlalchemy import text

from app.data.session import get_session


class AuditAction(StrEnum):
    REQUEUE = "requeue"
    CLEAR_IDEMPOTENT = "clear-idempotent"


class AuditStatus(StrEnum):
    CLEARED = "cleared"
    REQUEUED = "requeued"
    PUBLISH_FAILED = "publish_failed"
    ZOMBIE_ACKED = "zombie_acked"
    ALREADY_SUCCEEDED = "already_succeeded"


RUNTIME_DLQ_AUDIT_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS runtime_dlq_audit (
        id BIGSERIAL PRIMARY KEY,
        action TEXT NOT NULL,
        status TEXT NOT NULL,
        queue TEXT,
        queue_kind TEXT,
        message_ids JSONB,
        recovery_token TEXT,
        recovery_hint TEXT,
        cleared_inflight_count INT,
        requeued_count INT,
        operator TEXT,
        trace_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_dlq_audit_queue_idx
    ON runtime_dlq_audit (queue, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_dlq_audit_status_idx
    ON runtime_dlq_audit (status) WHERE status != 'requeued'
    """,
]


async def insert_audit_row(
    *, action: AuditAction, status: AuditStatus,
    queue: str | None, queue_kind: str | None,
    message_ids: list[str] | None,
    recovery_token: str | None, recovery_hint: str | None,
    cleared_inflight_count: int, requeued_count: int,
    operator: str | None, trace_id: str | None,
) -> int:
    async with get_session() as s:
        row = await s.execute(text(
            "INSERT INTO runtime_dlq_audit "
            "(action, status, queue, queue_kind, message_ids, "
            " recovery_token, recovery_hint, cleared_inflight_count, "
            " requeued_count, operator, trace_id) "
            "VALUES (:a, :s, :q, :qk, :mids::jsonb, :rt, :rh, :cic, :rc, "
            "        :op, :tid) RETURNING id"
        ), {
            "a": str(action), "s": str(status),
            "q": queue, "qk": queue_kind,
            "mids": json.dumps(message_ids) if message_ids else None,
            "rt": recovery_token, "rh": recovery_hint,
            "cic": cleared_inflight_count, "rc": requeued_count,
            "op": operator, "tid": trace_id,
        })
        await s.commit()
        return row.scalar()


async def update_audit_status(
    audit_id: int, status: AuditStatus,
    *, requeued_count: int | None = None,
    recovery_hint: str | None = None,
) -> None:
    async with get_session() as s:
        await s.execute(text(
            "UPDATE runtime_dlq_audit "
            "SET status=:s, updated_at=now(), "
            "    requeued_count=COALESCE(:rc, requeued_count), "
            "    recovery_hint=COALESCE(:rh, recovery_hint) "
            "WHERE id=:i"
        ), {"s": str(status), "rc": requeued_count, "rh": recovery_hint,
            "i": audit_id})
        await s.commit()
```

- [ ] **Step 6.4: Register the DDL in the runtime boot path**

Find the runtime startup hook that applies `RUNTIME_INFLIGHT_DDL` (`grep -rn "RUNTIME_INFLIGHT_DDL" apps/agent-service/app/runtime/`). Add `RUNTIME_DLQ_AUDIT_DDL` next to it. Likely site is `runtime/migrator.py` or `runtime/runtime.py`.

```python
from app.runtime.dlq_audit import RUNTIME_DLQ_AUDIT_DDL
# inside the bootstrap block that runs RUNTIME_INFLIGHT_DDL:
for ddl in RUNTIME_DLQ_AUDIT_DDL:
    await conn.execute(ddl)
```

- [ ] **Step 6.5: Audit test green**

Run: `uv run pytest apps/agent-service/tests/runtime/test_dlq_audit.py -v`
Expected: 1 passed.

- [ ] **Step 6.6: Failing test for admin nodes**

Create `apps/agent-service/tests/runtime/test_dlq_admin.py`:

```python
"""Phase 7b Gap 12: admin DLQ nodes (inspect / clear-idempotent / dry-run / requeue)."""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.dlq_audit import RUNTIME_DLQ_AUDIT_DDL
from app.runtime.errors import AlreadySucceededError
from app.runtime.inflight import RUNTIME_INFLIGHT_DDL


@pytest.fixture(autouse=True)
async def _setup():
    async with get_session() as s:
        for ddl in RUNTIME_INFLIGHT_DDL + RUNTIME_DLQ_AUDIT_DDL:
            await s.execute(text(ddl))
        await s.execute(text("DELETE FROM runtime_inflight"))
        await s.execute(text("DELETE FROM runtime_dlq_audit"))
        await s.commit()


@pytest.mark.asyncio
async def test_inspect_returns_peeked_rows():
    from app.nodes.dlq_admin import dlq_inspect_impl
    fake = [{
        "properties": {"headers": {"trace_id": "t1"}},
        "payload": '{"data_type":"x.Y","payload":{}}',
    }]
    with patch("app.nodes.dlq_admin._mgmt_client") as m:
        m.peek_messages = AsyncMock(return_value=fake)
        rows = await dlq_inspect_impl(queue="durable_x_y_dlx", limit=5,
                                      queue_kind="dlq")
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "t1"


@pytest.mark.asyncio
async def test_clear_idempotent_edge_succeeded_returns_409():
    from app.nodes.dlq_admin import dlq_clear_idempotent_impl
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) "
            "VALUES ('e1', 'k1', 't', 'succeeded', 1)"
        ))
        await s.commit()
    body = {"by": "edge_idempotent", "edge_id": "e1", "idempotent_key": "k1"}
    resp = await dlq_clear_idempotent_impl(body, operator="op-x")
    assert resp["status_code"] == 409
    assert "AlreadySucceeded" in resp["error"]


@pytest.mark.asyncio
async def test_clear_idempotent_trace_skips_succeeded():
    from app.nodes.dlq_admin import dlq_clear_idempotent_impl
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts, trace_id) "
            "VALUES ('e1', 'k1', 't', 'succeeded', 1, 'trA'),"
            "       ('e2', 'k2', 't', 'failed', 1, 'trA')"
        ))
        await s.commit()
    body = {"by": "trace_id", "trace_id": "trA"}
    resp = await dlq_clear_idempotent_impl(body, operator="op-x")
    assert resp["deleted"] == 1
    assert resp["skipped_succeeded"] == 1


@pytest.mark.asyncio
async def test_requeue_zombie_path_acks_without_publish():
    """If delete_inflight raises AlreadySucceededError, the requeue path
    must ack the DLQ message and write a 'zombie_acked' audit row."""
    from app.nodes.dlq_admin import dlq_requeue_impl
    fake_msg = type("M", (), {
        "body": b'{"data":{"id":"x"},"data_type":"x.Y","origin_app":"agent-service","lane":null,"trace_id":"t1","edge_id":"e1","idempotent_key":"k1"}',
        "ack": AsyncMock(),
        "nack": AsyncMock(),
    })()
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch("app.nodes.dlq_admin.delete_inflight",
               new=AsyncMock(side_effect=AlreadySucceededError(edge_id="e1", idempotent_key="k1"))), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=True)
        body = {"queue": "q", "queue_kind": "dlq", "limit": 1, "clear_idempotent": True}
        resp = await dlq_requeue_impl(body, operator="op-x")
    fake_msg.ack.assert_awaited_once()
    mq.publish_with_confirm.assert_not_awaited()
    assert resp["zombie_acked"] == 1


@pytest.mark.asyncio
async def test_requeue_publish_failed_nacks_and_audits():
    from app.nodes.dlq_admin import dlq_requeue_impl
    fake_msg = type("M", (), {
        "body": b'{"data":{"id":"x"},"data_type":"x.Y","origin_app":"agent-service","lane":null,"trace_id":"t1","edge_id":"e1","idempotent_key":"k1","origin_queue":"q"}',
        "ack": AsyncMock(),
        "nack": AsyncMock(),
    })()
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch("app.nodes.dlq_admin.delete_inflight", new=AsyncMock()), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=False)
        body = {"queue": "q", "queue_kind": "dlq", "limit": 1, "clear_idempotent": True}
        resp = await dlq_requeue_impl(body, operator="op-x")
    fake_msg.nack.assert_awaited_once()
    fake_msg.ack.assert_not_awaited()
    assert resp["publish_failed"] == 1


@pytest.mark.asyncio
async def test_dry_run_does_not_mutate():
    from app.nodes.dlq_admin import dlq_dry_run_impl
    with patch("app.nodes.dlq_admin._mgmt_client") as m:
        m.peek_messages = AsyncMock(return_value=[
            {"payload": '{"edge_id":"e1","idempotent_key":"k1"}'}
        ])
        body = {"queue": "q", "queue_kind": "dlq", "limit": 5}
        plan = await dlq_dry_run_impl(body)
    assert "plan" in plan
    assert len(plan["plan"]) == 1
```

- [ ] **Step 6.7: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_dlq_admin.py -v`
Expected: FAIL — module missing.

- [ ] **Step 6.8: Implement `nodes/dlq_admin.py`**

Create `apps/agent-service/app/nodes/dlq_admin.py`:

```python
"""Phase 7b Gap 12: admin DLQ replay nodes.

Each function is paired with a Source.http(...) admin route in
wiring/admin.py. The 6-step requeue protocol implementation lives in
dlq_requeue_impl below; see spec §3.2.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.infra.rabbitmq import ALL_ROUTES, current_lane, mq
from app.runtime.dlq_audit import (
    AuditAction,
    AuditStatus,
    insert_audit_row,
    update_audit_status,
)
from app.runtime.errors import AlreadySucceededError
from app.runtime.inflight import delete_inflight
from app.runtime.rabbitmq_management import RabbitMQManagementClient

logger = logging.getLogger(__name__)
_mgmt_client = RabbitMQManagementClient.from_env()


# ---------------------------------------------------------------------------
# inspect

async def dlq_inspect_impl(*, queue: str, limit: int = 20,
                           queue_kind: str = "dlq") -> list[dict[str, Any]]:
    raw = await _mgmt_client.peek_messages(queue=queue, limit=limit)
    out = []
    for m in raw:
        headers = (m.get("properties") or {}).get("headers") or {}
        try:
            payload_obj = json.loads(m.get("payload", "{}"))
        except Exception:
            payload_obj = {"_unparseable": True}
        out.append({
            "trace_id": headers.get("trace_id"),
            "data_type": payload_obj.get("data_type"),
            "payload": payload_obj.get("payload") or payload_obj.get("data"),
            "attempts": headers.get("x-delivery-count"),
            "first_failed_at": None,  # filled by JOIN runtime_inflight in v2
        })
    return out


# ---------------------------------------------------------------------------
# clear-idempotent

async def dlq_clear_idempotent_impl(body: dict[str, Any], *, operator: str | None) -> dict[str, Any]:
    by = body.get("by")
    try:
        outcome = await delete_inflight(
            by=by,
            trace_id=body.get("trace_id"),
            edge_id=body.get("edge_id"),
            idempotent_key=body.get("idempotent_key"),
        )
    except AlreadySucceededError as e:
        await insert_audit_row(
            action=AuditAction.CLEAR_IDEMPOTENT,
            status=AuditStatus.ALREADY_SUCCEEDED,
            queue=None, queue_kind=None, message_ids=None,
            recovery_token=None,
            recovery_hint=f"edge_id={e.edge_id} idempotent_key={e.idempotent_key}",
            cleared_inflight_count=0, requeued_count=0,
            operator=operator, trace_id=body.get("trace_id"),
        )
        return {"status_code": 409, "error": "AlreadySucceeded",
                "edge_id": e.edge_id, "idempotent_key": e.idempotent_key}
    audit_id = await insert_audit_row(
        action=AuditAction.CLEAR_IDEMPOTENT,
        status=AuditStatus.CLEARED,
        queue=None, queue_kind=None, message_ids=None,
        recovery_token=None, recovery_hint=None,
        cleared_inflight_count=outcome.deleted,
        requeued_count=0, operator=operator, trace_id=body.get("trace_id"),
    )
    return {
        "status_code": 200,
        "deleted": outcome.deleted,
        "skipped_succeeded": outcome.skipped_succeeded,
        "audit_id": audit_id,
    }


# ---------------------------------------------------------------------------
# dry-run

async def dlq_dry_run_impl(body: dict[str, Any]) -> dict[str, Any]:
    queue = body["queue"]
    limit = body.get("limit", 20)
    raw = await _mgmt_client.peek_messages(queue=queue, limit=limit)
    plan = []
    for m in raw:
        try:
            payload_obj = json.loads(m.get("payload", "{}"))
        except Exception:
            payload_obj = {}
        plan.append({
            "message_id": (m.get("properties") or {}).get("message_id"),
            "will_clear_idempotent": True,
            "target_queue": payload_obj.get("origin_queue") or queue.replace("-dlx", ""),
        })
    return {"plan": plan}


# ---------------------------------------------------------------------------
# requeue (6-step transaction-like)

async def _basic_get_one(queue: str):
    """Wrap aio_pika.Channel.basic_get(no_ack=False). Implementation
    delegated to runtime/durable infrastructure to share connection.
    """
    from app.infra.rabbitmq import basic_get  # see Step 6.9 — add helper
    return await basic_get(queue, no_ack=False)


async def dlq_requeue_impl(body: dict[str, Any], *, operator: str | None) -> dict[str, Any]:
    queue = body["queue"]
    limit = body.get("limit", 1)
    clear = body.get("clear_idempotent", False)

    requeued = 0
    publish_failed = 0
    zombie_acked = 0

    for _ in range(limit):
        msg = await _basic_get_one(queue)
        if msg is None:
            break  # queue empty

        try:
            envelope = json.loads(msg.body)
        except Exception:
            await msg.nack(requeue=True)
            continue

        msg_id = envelope.get("message_id") or str(envelope.get("trace_id") or "")
        # step 2: audit cleared row first
        audit_id = await insert_audit_row(
            action=AuditAction.REQUEUE, status=AuditStatus.CLEARED,
            queue=queue, queue_kind=body.get("queue_kind", "dlq"),
            message_ids=[msg_id], recovery_token=msg_id,
            recovery_hint=None, cleared_inflight_count=0,
            requeued_count=0, operator=operator,
            trace_id=envelope.get("trace_id"),
        )

        # step 3: clear idempotent (edge_idempotent precise mode)
        if clear:
            try:
                await delete_inflight(
                    by="edge_idempotent",
                    edge_id=envelope.get("edge_id"),
                    idempotent_key=envelope.get("idempotent_key"),
                )
            except AlreadySucceededError:
                await update_audit_status(
                    audit_id, AuditStatus.ZOMBIE_ACKED,
                    recovery_hint="inflight already succeeded; DLQ message acked as zombie",
                )
                await msg.ack()
                zombie_acked += 1
                continue

        # step 4: publish-with-confirm to original queue
        target_queue = envelope.get("origin_queue") or queue.replace("-dlx", "")
        route = ALL_ROUTES.get(target_queue)
        if route is None:
            await update_audit_status(
                audit_id, AuditStatus.PUBLISH_FAILED,
                recovery_hint=f"no Route for target_queue={target_queue!r}",
            )
            await msg.nack(requeue=True)
            publish_failed += 1
            continue
        body_payload = envelope.get("data") or envelope.get("payload")
        confirmed = await mq.publish_with_confirm(
            route, body_payload,
            headers=envelope.get("headers") or {},
            lane=envelope.get("lane") or current_lane(),
        )
        if not confirmed:
            await update_audit_status(
                audit_id, AuditStatus.PUBLISH_FAILED,
                recovery_hint="publish_with_confirm returned False; "
                              "DLQ message nacked back; idempotent already cleared",
            )
            await msg.nack(requeue=True)
            publish_failed += 1
            continue

        # step 5 + 6
        await update_audit_status(audit_id, AuditStatus.REQUEUED, requeued_count=1)
        await msg.ack()
        requeued += 1

    return {
        "status_code": 200,
        "requeued": requeued,
        "publish_failed": publish_failed,
        "zombie_acked": zombie_acked,
    }
```

- [ ] **Step 6.9: Add `basic_get` helper to `app/infra/rabbitmq.py`**

Add (placement: near other publish/consume helpers):

```python
async def basic_get(queue: str, *, no_ack: bool = False):
    """Phase 7b Gap 12: blocking basic.get(no_ack=False) for DLQ replay.

    Returns aio_pika.IncomingMessage or None if queue empty. Caller is
    responsible for ack()/nack(requeue=...) on the returned message.
    """
    channel = await _get_channel()  # however the existing module obtains a channel
    queue_obj = await channel.declare_queue(queue, passive=True)
    return await queue_obj.get(timeout=5, fail=False, no_ack=no_ack)
```

> Concrete implementation depends on the existing aio_pika integration in `app/infra/rabbitmq.py`. Adapt the channel acquisition to whatever pattern the existing `mq.publish` uses.

- [ ] **Step 6.10: Define request/response Data classes**

Create `apps/agent-service/app/domain/dlq_admin_events.py`:

```python
from app.runtime.data import Data, Key


class DlqInspectRequest(Data):
    request_id: Key[str]
    queue: str
    limit: int = 20
    queue_kind: str = "dlq"


class DlqInspectResponse(Data):
    request_id: Key[str]
    rows: list[dict]


class DlqClearIdempotentRequest(Data):
    request_id: Key[str]
    by: str
    trace_id: str | None = None
    edge_id: str | None = None
    idempotent_key: str | None = None


class DlqClearIdempotentResponse(Data):
    request_id: Key[str]
    deleted: int = 0
    skipped_succeeded: int = 0
    error: str | None = None
    edge_id: str | None = None
    idempotent_key: str | None = None
    status_code: int = 200


class DlqDryRunRequest(Data):
    request_id: Key[str]
    queue: str
    limit: int = 20
    queue_kind: str = "dlq"


class DlqDryRunResponse(Data):
    request_id: Key[str]
    plan: list[dict]


class DlqRequeueRequest(Data):
    request_id: Key[str]
    queue: str
    queue_kind: str = "dlq"
    limit: int = 20
    clear_idempotent: bool = False


class DlqRequeueResponse(Data):
    request_id: Key[str]
    requeued: int = 0
    publish_failed: int = 0
    zombie_acked: int = 0
    status_code: int = 200
```

- [ ] **Step 6.10b: Add `operator_var` to middleware**

The X-Operator HTTP header is needed by `dlq_clear_idempotent` and `dlq_requeue` for the audit row. Modify `apps/agent-service/app/api/middleware.py` — declare alongside `lane_var` / `trace_id_var`:

```python
from contextvars import ContextVar

operator_var: ContextVar[str | None] = ContextVar("operator", default=None)
header_vars["operator"] = operator_var  # if there is a header_vars dict registry
```

If the existing http source middleware iterates `header_vars` to bind incoming headers (search for `lane_var.set` / `trace_id_var.set` to confirm the pattern), `operator_var` will pick up `X-Operator` automatically. Otherwise add an explicit `operator_var.set(request.headers.get("X-Operator"))` call where `lane_var` is set.

- [ ] **Step 6.10c: Wire 4 admin nodes + endpoints**

Modify `apps/agent-service/app/wiring/admin.py`. Add these node + wire pairs (placement: after the existing admin wires, before file end):

```python
from app.api.middleware import operator_var
from app.domain.dlq_admin_events import (
    DlqClearIdempotentRequest, DlqClearIdempotentResponse,
    DlqDryRunRequest, DlqDryRunResponse,
    DlqInspectRequest, DlqInspectResponse,
    DlqRequeueRequest, DlqRequeueResponse,
)
from app.nodes.dlq_admin import (
    dlq_clear_idempotent_impl,
    dlq_dry_run_impl,
    dlq_inspect_impl,
    dlq_requeue_impl,
)
from app.runtime import node, wire
from app.runtime.source import Source


@node
async def dlq_inspect_node(req: DlqInspectRequest) -> DlqInspectResponse:
    rows = await dlq_inspect_impl(
        queue=req.queue, limit=req.limit, queue_kind=req.queue_kind,
    )
    return DlqInspectResponse(request_id=req.request_id, rows=rows)


wire(DlqInspectRequest).to(dlq_inspect_node).from_(
    Source.http("/admin/dlq/inspect", method="POST", response=True),
)


@node
async def dlq_clear_idempotent_node(
    req: DlqClearIdempotentRequest,
) -> DlqClearIdempotentResponse:
    body = {
        "by": req.by,
        "trace_id": req.trace_id,
        "edge_id": req.edge_id,
        "idempotent_key": req.idempotent_key,
    }
    resp = await dlq_clear_idempotent_impl(body, operator=operator_var.get())
    return DlqClearIdempotentResponse(
        request_id=req.request_id,
        deleted=resp.get("deleted", 0),
        skipped_succeeded=resp.get("skipped_succeeded", 0),
        error=resp.get("error"),
        edge_id=resp.get("edge_id"),
        idempotent_key=resp.get("idempotent_key"),
        status_code=resp.get("status_code", 200),
    )


wire(DlqClearIdempotentRequest).to(dlq_clear_idempotent_node).from_(
    Source.http("/admin/dlq/clear-idempotent", method="POST", response=True),
)


@node
async def dlq_dry_run_node(req: DlqDryRunRequest) -> DlqDryRunResponse:
    body = {
        "queue": req.queue, "limit": req.limit, "queue_kind": req.queue_kind,
    }
    resp = await dlq_dry_run_impl(body)
    return DlqDryRunResponse(request_id=req.request_id, plan=resp["plan"])


wire(DlqDryRunRequest).to(dlq_dry_run_node).from_(
    Source.http("/admin/dlq/dry-run", method="POST", response=True),
)


@node
async def dlq_requeue_node(req: DlqRequeueRequest) -> DlqRequeueResponse:
    body = {
        "queue": req.queue, "queue_kind": req.queue_kind,
        "limit": req.limit, "clear_idempotent": req.clear_idempotent,
    }
    resp = await dlq_requeue_impl(body, operator=operator_var.get())
    return DlqRequeueResponse(
        request_id=req.request_id,
        requeued=resp.get("requeued", 0),
        publish_failed=resp.get("publish_failed", 0),
        zombie_acked=resp.get("zombie_acked", 0),
        status_code=resp.get("status_code", 200),
    )


wire(DlqRequeueRequest).to(dlq_requeue_node).from_(
    Source.http("/admin/dlq/requeue", method="POST", response=True),
)
```

- [ ] **Step 6.11: Run admin tests**

Run: `uv run pytest apps/agent-service/tests/runtime/test_dlq_admin.py -v`
Expected: 6 passed.

- [ ] **Step 6.12: Lint**

Run: `uv run ruff check apps/agent-service/app/runtime/dlq_audit.py apps/agent-service/app/nodes/dlq_admin.py apps/agent-service/app/wiring/admin.py apps/agent-service/app/domain/dlq_admin_events.py apps/agent-service/tests/runtime/test_dlq_audit.py apps/agent-service/tests/runtime/test_dlq_admin.py`

- [ ] **Step 6.13: Commit**

```bash
git add apps/agent-service/app/runtime/dlq_audit.py \
        apps/agent-service/app/runtime/migrator.py \
        apps/agent-service/app/nodes/dlq_admin.py \
        apps/agent-service/app/wiring/admin.py \
        apps/agent-service/app/domain/dlq_admin_events.py \
        apps/agent-service/app/api/middleware.py \
        apps/agent-service/app/infra/rabbitmq.py \
        apps/agent-service/tests/runtime/test_dlq_audit.py \
        apps/agent-service/tests/runtime/test_dlq_admin.py
git commit -m "$(cat <<'EOF'
feat(runtime): admin DLQ endpoints + audit_log (6-step transaction-like requeue)

Phase 7b Gap 12 step 2/3. /admin/dlq/{inspect,clear-idempotent,dry-run,
requeue} live behind Source.http via wiring/admin. Requeue protocol:
basic_get(no_ack=False) → audit cleared row → delete_inflight (zombie
detected via AlreadySucceededError → ack + audit zombie_acked) →
publish_with_confirm (false → nack + audit publish_failed) → audit
requeued → ack. clear_idempotent in trace_id mode reports
skipped_succeeded count without raising.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7 — Gap 12: Makefile dlq targets + runbook

**Goal:** Operations-facing surface — `make dlq-{inspect,replay,dry-run}` invokes the admin endpoints; runbook documents the decision tree.

**Files:**
- Modify: top-level `Makefile`
- Create: `docs/runbooks/dlq-replay.md`

- [ ] **Step 7.1: Add Makefile targets**

Append to `Makefile` (top-level):

```makefile
dlq-inspect:  ## DLQ inspect: QUEUE=<name> [LIMIT=20] [KIND=dlq|review]
	@scripts/http.sh POST $(PAAS_API)/admin/dlq/inspect \
	  -d '{"queue":"$(QUEUE)","limit":$(or $(LIMIT),20),"queue_kind":"$(or $(KIND),dlq)"}'

dlq-replay:   ## DLQ replay: QUEUE=<name> [LIMIT=N] [CLEAR=true]
	@scripts/http.sh POST $(PAAS_API)/admin/dlq/requeue \
	  -H "X-Operator: $$(git config user.name)" \
	  -d '{"queue":"$(QUEUE)","queue_kind":"dlq","limit":$(or $(LIMIT),20),"clear_idempotent":$(or $(CLEAR),false)}'

dlq-dry-run:  ## DLQ dry-run: QUEUE=<name> [LIMIT=20] [KIND=dlq|review]
	@scripts/http.sh POST $(PAAS_API)/admin/dlq/dry-run \
	  -d '{"queue":"$(QUEUE)","limit":$(or $(LIMIT),20),"queue_kind":"$(or $(KIND),dlq)"}'
```

Verify integration: `grep -n "^dlq-inspect:\|^dlq-replay:\|^dlq-dry-run:" Makefile` → 3 matches.

- [ ] **Step 7.2: Create runbook**

Create `docs/runbooks/dlq-replay.md`:

```markdown
# DLQ Replay Runbook

## When does a message land in DLQ?

A durable consumer raised an exception that:
- Was not classified as DuplicateData with `on_error="ignore-duplicate"`
- Was not classified as NeedsReview with `on_error="manual-review"`
- Exhausted the wire's `.retry(...)` budget (or had no retry policy)

The broker routed the original message to its DLX → DLQ
(`durable_<data>_<consumer>-dlx`).

## Decision tree

1. **Inspect**: `make dlq-inspect QUEUE=<name>`
   Look at the topmost message — what's the `data_type`? `last_error`?
   `attempts`? `trace_id`?

2. **Diagnose root cause** (out of band — check logs, code, infra).

3. **Decide action**:
   - **Bug fixed, replay safe** → goto step 4 (replay).
   - **Bug not yet fixed** → leave DLQ alone (do NOT delete; messages
     are evidence).
   - **Replay would create duplicate side effect (consumer not
     idempotent)** → DO NOT use `CLEAR=true`. Use targeted
     `clear-idempotent by=trace_id` per individual message after
     verifying consumer state.

4. **Dry-run first** (recommended for >1 message):
   `make dlq-dry-run QUEUE=<name>`
   Verifies what would be cleared and where it would be re-published.

5. **Replay**: `make dlq-replay QUEUE=<name> CLEAR=true LIMIT=10`
   - `CLEAR=true` clears `runtime_inflight` rows for messages being
     replayed (without it, the consumer dedup will silently skip
     re-delivery — this is the historical "replay no-op" bug).
   - `LIMIT=N` caps the batch.
   - Audit row written to `runtime_dlq_audit`; `X-Operator` header
     auto-populated from `git config user.name`.

## Failure recovery

If `make dlq-replay` reports `publish_failed > 0`:
- Original DLQ messages have been NACKed back (still in DLQ, ready for
  another attempt once the broker is healthy again).
- `runtime_inflight` rows for those messages have been cleared
  (idempotent for retry).
- `runtime_dlq_audit` rows show `status='publish_failed'` with
  `recovery_hint`. Inspect: `SELECT * FROM runtime_dlq_audit WHERE
  status='publish_failed' ORDER BY created_at DESC LIMIT 10;`

If `zombie_acked > 0`:
- Those messages were the "second-replay" zombies (consumer had
  already succeeded between attempts). They have been acked silently.
- Audit row `status='zombie_acked'`; consumer side already mark_succeeded
  — no action needed.

## Manual-review queues

Same `make dlq-*` commands work; pass `KIND=review`. Example:
`make dlq-inspect QUEUE=durable_<data>_<consumer>_review KIND=review`

To **dispose** of a review message (operator decides "ignore"):
`make dlq-replay QUEUE=<review queue> KIND=review CLEAR=false LIMIT=1` —
the message is acked but NOT re-published (CLEAR=false leaves
`runtime_inflight` row in `state='review'`, claim_inflight will skip).

To **re-process** a review message after fixing the underlying issue:
1. `make dlq-replay QUEUE=<review queue> KIND=review CLEAR=true` —
   clears the `state='review'` row from `runtime_inflight`.
2. Manually re-publish the message body to the **original durable
   queue** (NOT the review queue) — see SOP-XXXX for credentials.

## Routine checks

Add to oncall checklist (weekly):
- Any non-empty review queues? `make dlq-inspect KIND=review` per known
  review queue name.
- Any DLQ over `<X>` deep? Indicates an unfixed bug in production.
- Any `runtime_dlq_audit` rows with `status NOT IN ('requeued',
  'zombie_acked')` older than 24h? Stale `cleared` / `publish_failed` —
  needs operator intervention.
```

- [ ] **Step 7.3: Verify the gate file checks pass locally**

Run:
```bash
grep -q "^dlq-inspect:" Makefile && echo OK
grep -q "^dlq-replay:" Makefile && echo OK
grep -q "^dlq-dry-run:" Makefile && echo OK
test -f docs/runbooks/dlq-replay.md && echo OK
```
Expected: 4× OK.

- [ ] **Step 7.4: Commit**

```bash
git add Makefile docs/runbooks/dlq-replay.md
git commit -m "$(cat <<'EOF'
feat(ops): Makefile dlq targets + runbook

Phase 7b Gap 12 step 3/3. dlq-inspect/dlq-replay/dlq-dry-run wrap the
admin endpoints via scripts/http.sh; X-Operator header is populated
from git config. Runbook documents decision tree, failure recovery
(publish_failed / zombie_acked semantics), and manual-review queue
operations.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8 — Gap 8: `runtime_outbox` table + `transactional_emit`

**Goal:** Schema + business API. After this task, `transactional_emit` is callable but no business code uses it yet, and no dispatcher consumes the rows.

**Files:**
- Create: `apps/agent-service/app/runtime/outbox.py`
- Modify: `apps/agent-service/app/runtime/migrator.py` (or boot path) — register `RUNTIME_OUTBOX_DDL`
- Modify: `apps/agent-service/app/runtime/__init__.py` — re-export `transactional_emit`
- Test: `apps/agent-service/tests/runtime/test_outbox.py`

- [ ] **Step 8.1: Failing test for `OutboxEmitter.append` + transactional rollback**

Create `apps/agent-service/tests/runtime/test_outbox.py`:

```python
"""Phase 7b Gap 8: OutboxEmitter + transactional_emit."""
import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, Key
from app.runtime.outbox import RUNTIME_OUTBOX_DDL, transactional_emit


class _D(Data):
    id: Key[str]
    val: str = ""


@pytest.fixture(autouse=True)
async def _setup(monkeypatch):
    async with get_session() as s:
        for ddl in RUNTIME_OUTBOX_DDL:
            await s.execute(text(ddl))
        await s.execute(text("DELETE FROM runtime_outbox"))
        await s.commit()


@pytest.mark.asyncio
async def test_append_writes_pending_row():
    async with get_session() as s:
        async with transactional_emit(s) as emitter:
            await emitter.append(_D(id="x", val="v1"))
        await s.commit()
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT data_type, payload_json::text, state, origin_app FROM runtime_outbox"
        ))).mappings().all()
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["data_type"].endswith("._D")
    assert "x" in rows[0]["payload_json"]
    assert rows[0]["origin_app"] in ("agent-service", "default")  # APP_NAME or DEFAULT_APP


@pytest.mark.asyncio
async def test_session_rollback_drops_outbox_row():
    """Demonstrates the core property: outbox row + business write are atomic."""
    try:
        async with get_session() as s:
            async with transactional_emit(s) as emitter:
                await emitter.append(_D(id="rollback-me"))
            raise RuntimeError("simulate business error after append")
    except RuntimeError:
        pass
    async with get_session() as s:
        n = (await s.execute(text("SELECT count(*) FROM runtime_outbox"))).scalar()
    assert n == 0


@pytest.mark.asyncio
async def test_lane_uses_current_lane_helper(monkeypatch):
    """Append must call current_lane(), not lane_var.get() directly."""
    monkeypatch.setenv("LANE", "feat-x")
    async with get_session() as s:
        async with transactional_emit(s) as emitter:
            await emitter.append(_D(id="lx"))
        await s.commit()
    async with get_session() as s:
        lane = (await s.execute(text(
            "SELECT lane FROM runtime_outbox WHERE payload_json->>'id'='lx'"
        ))).scalar()
    assert lane == "feat-x"


@pytest.mark.asyncio
async def test_prod_lane_normalizes_to_null(monkeypatch):
    monkeypatch.delenv("LANE", raising=False)
    async with get_session() as s:
        async with transactional_emit(s) as emitter:
            await emitter.append(_D(id="prod"))
        await s.commit()
    async with get_session() as s:
        lane = (await s.execute(text(
            "SELECT lane FROM runtime_outbox WHERE payload_json->>'id'='prod'"
        ))).scalar()
    assert lane is None
```

- [ ] **Step 8.2: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_outbox.py -v`
Expected: FAIL — module missing.

- [ ] **Step 8.3: Implement `runtime/outbox.py`**

Create `apps/agent-service/app/runtime/outbox.py`:

```python
"""Phase 7b Gap 8: outbox pattern — atomic DB-write + emit.

Business mutation nodes use `async with transactional_emit(session)`
INSIDE their `async with get_session()` block. The append writes a
`runtime_outbox` row in the same transaction; commit makes it visible
and the dispatcher picks it up to fire `emit(data)`.

Lane normalization MUST go through `current_lane()` (infra/rabbitmq.py)
so the dispatcher SELECT (which also uses `current_lane()`) picks up
its own rows. Reading lane_var.get() bare from background paths (e.g.
cron, retries) returns None even when LANE env is set.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.middleware import trace_id_var
from app.infra.rabbitmq import current_lane
from app.runtime.data import Data
from app.runtime.placement import DEFAULT_APP


RUNTIME_OUTBOX_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS runtime_outbox (
        id              BIGSERIAL PRIMARY KEY,
        data_type       TEXT NOT NULL,
        payload_json    JSONB NOT NULL,
        origin_app      TEXT NOT NULL,
        lane            TEXT,
        trace_id        TEXT,
        state           TEXT NOT NULL DEFAULT 'pending',
        attempts        INT  NOT NULL DEFAULT 0,
        last_error      TEXT,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        dispatched_at   TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_outbox_pending_idx
    ON runtime_outbox (state, next_attempt_at)
    WHERE state = 'pending'
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_outbox_trace_idx
    ON runtime_outbox (trace_id) WHERE trace_id IS NOT NULL
    """,
]


def _current_app() -> str:
    return os.getenv("APP_NAME") or DEFAULT_APP


class OutboxEmitter:
    """Append-only emitter bound to a caller-provided session."""

    def __init__(self, session: AsyncSession) -> None:
        if session is None:
            raise TypeError("transactional_emit requires an AsyncSession")
        self._session = session

    async def append(self, data: Data) -> None:
        cls = type(data)
        data_type = f"{cls.__module__}.{cls.__qualname__}"
        payload_json = json.dumps(data.model_dump(mode="json"))
        await self._session.execute(text(
            "INSERT INTO runtime_outbox "
            "(data_type, payload_json, origin_app, lane, trace_id) "
            "VALUES (:dt, :pj::jsonb, :app, :lane, :tid)"
        ), {
            "dt": data_type,
            "pj": payload_json,
            "app": _current_app(),
            "lane": current_lane(),
            "tid": trace_id_var.get(),
        })


@asynccontextmanager
async def transactional_emit(session: AsyncSession) -> AsyncIterator[OutboxEmitter]:
    """Context manager that yields an OutboxEmitter bound to ``session``.

    Does NOT commit/rollback — the caller's session context owns commit
    semantics, which is the whole point of the outbox pattern.
    """
    yield OutboxEmitter(session)
```

- [ ] **Step 8.4: Bootstrap DDL**

Find where `RUNTIME_INFLIGHT_DDL` and `RUNTIME_DLQ_AUDIT_DDL` are applied. Add `RUNTIME_OUTBOX_DDL` next to them.

- [ ] **Step 8.5: Re-export from runtime package**

Modify `apps/agent-service/app/runtime/__init__.py`:

```python
from app.runtime.outbox import transactional_emit
```

Add `"transactional_emit"` to `__all__`.

- [ ] **Step 8.6: Verify outbox tests pass**

Run: `uv run pytest apps/agent-service/tests/runtime/test_outbox.py -v`
Expected: 4 passed.

- [ ] **Step 8.7: Lint**

Run: `uv run ruff check apps/agent-service/app/runtime/outbox.py apps/agent-service/app/runtime/__init__.py apps/agent-service/tests/runtime/test_outbox.py`

- [ ] **Step 8.8: Commit**

```bash
git add apps/agent-service/app/runtime/outbox.py \
        apps/agent-service/app/runtime/__init__.py \
        apps/agent-service/app/runtime/migrator.py \
        apps/agent-service/tests/runtime/test_outbox.py
git commit -m "$(cat <<'EOF'
feat(runtime): runtime_outbox table + transactional_emit

Phase 7b Gap 8 step 1/3. transactional_emit binds an OutboxEmitter to
the caller's AsyncSession; .append() inserts a pending runtime_outbox
row in the same business transaction. lane is normalized through
current_lane() (infra/rabbitmq.py:122) — same helper the dispatcher
will use in step 2/3 — so writer and reader see the same lane value
even on background paths where lane_var is unset.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9 — Gap 8: dispatcher loop + lifespan dual-entry

**Goal:** Background loop that drains `runtime_outbox` by calling `emit(data)` for each row. Per-deployment, per-lane filtering ensures prod doesn't drain dev rows.

**Files:**
- Create: `apps/agent-service/app/runtime/outbox_dispatcher.py`
- Modify: `apps/agent-service/app/runtime/runtime.py` (start task in `Runtime.run`)
- Modify: `apps/agent-service/app/main.py` (start task in lifespan — dual-entry)
- Test: `apps/agent-service/tests/runtime/test_outbox_dispatcher.py`

- [ ] **Step 9.1: Failing test for dispatcher loop**

Create `apps/agent-service/tests/runtime/test_outbox_dispatcher.py`:

```python
"""Phase 7b Gap 8: outbox dispatcher_loop unit tests.

Mocks `emit` / `deserialize_data` / `bind_propagation_from_payload`;
NEVER mocks mq.* — dispatcher's only output is a call to emit().
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.outbox import RUNTIME_OUTBOX_DDL
from app.runtime.outbox_dispatcher import (
    _drain_once,
    bind_propagation_from_payload,
    deserialize_data,
)


@pytest.fixture(autouse=True)
async def _setup():
    async with get_session() as s:
        for ddl in RUNTIME_OUTBOX_DDL:
            await s.execute(text(ddl))
        await s.execute(text("DELETE FROM runtime_outbox"))
        await s.commit()


async def _seed(*, app="agent-service", lane=None,
                data_type="x.Y", payload=None, trace_id="tr"):
    payload = payload or {"id": "x"}
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_outbox "
            "(data_type, payload_json, origin_app, lane, trace_id) "
            "VALUES (:dt, :pj::jsonb, :a, :l, :t)"
        ), {"dt": data_type, "pj": json.dumps(payload), "a": app, "l": lane, "t": trace_id})
        await s.commit()


@pytest.mark.asyncio
async def test_drain_dispatches_one_pending_row():
    await _seed()
    fake_data = object()
    with patch("app.runtime.outbox_dispatcher.emit", new=AsyncMock()) as e, \
         patch("app.runtime.outbox_dispatcher.deserialize_data",
               return_value=fake_data) as d:
        await _drain_once(app="agent-service", lane=None)
    e.assert_awaited_once_with(fake_data)
    async with get_session() as s:
        state = (await s.execute(text(
            "SELECT state FROM runtime_outbox LIMIT 1"
        ))).scalar()
    assert state == "dispatched"


@pytest.mark.asyncio
async def test_drain_skips_rows_for_other_app():
    await _seed(app="other")
    with patch("app.runtime.outbox_dispatcher.emit", new=AsyncMock()) as e:
        await _drain_once(app="agent-service", lane=None)
    e.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_skips_rows_for_other_lane():
    await _seed(lane="feat-x")
    with patch("app.runtime.outbox_dispatcher.emit", new=AsyncMock()) as e:
        await _drain_once(app="agent-service", lane=None)  # prod dispatcher
    e.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_lane_match_succeeds():
    await _seed(lane="feat-x")
    with patch("app.runtime.outbox_dispatcher.emit", new=AsyncMock()) as e:
        await _drain_once(app="agent-service", lane="feat-x")
    e.assert_awaited_once()


@pytest.mark.asyncio
async def test_emit_failure_increments_attempts():
    await _seed()
    with patch("app.runtime.outbox_dispatcher.emit",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        await _drain_once(app="agent-service", lane=None)
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT state, attempts, last_error FROM runtime_outbox LIMIT 1"
        ))).mappings().first()
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert "boom" in (row["last_error"] or "")


@pytest.mark.asyncio
async def test_propagation_bound_before_emit():
    """bind_propagation_from_payload must run BEFORE emit() so consumers
    see the right trace_id/lane."""
    seen = {}

    async def _fake_emit(data):
        from app.api.middleware import lane_var, trace_id_var
        seen["lane"] = lane_var.get()
        seen["trace_id"] = trace_id_var.get()

    await _seed(lane="feat-x", trace_id="tr-99")
    with patch("app.runtime.outbox_dispatcher.emit", side_effect=_fake_emit), \
         patch("app.runtime.outbox_dispatcher.deserialize_data",
               return_value=object()):
        await _drain_once(app="agent-service", lane="feat-x")
    assert seen["lane"] == "feat-x"
    assert seen["trace_id"] == "tr-99"
```

- [ ] **Step 9.2: Run test to verify it fails**

Run: `uv run pytest apps/agent-service/tests/runtime/test_outbox_dispatcher.py -v`
Expected: FAIL — module missing.

- [ ] **Step 9.3: Implement `outbox_dispatcher.py`**

Create `apps/agent-service/app/runtime/outbox_dispatcher.py`:

```python
"""Phase 7b Gap 8: outbox dispatcher loop.

The dispatcher's ONLY job is to call `emit(data)` once per pending
runtime_outbox row, then mark the row dispatched. It does NOT publish
to RabbitMQ directly — emit() owns the wire fan-out (in-process /
durable / debounce / sink). At-least-once: if emit() succeeds and the
DB UPDATE crashes mid-flight, the next loop will pick the row up
again. Consumer-side runtime_inflight dedup absorbs the repeat for
durable wires; in-process wires must be side-effect-free or
self-idempotent (see spec §4.7 + §4.5.2).

Filter: SELECT WHERE origin_app = APP_NAME AND lane IS NOT DISTINCT
FROM current_lane() — prod and dev-lane pods do not race for the same
row.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
from typing import Any

from sqlalchemy import text

from app.api.middleware import lane_var, trace_id_var
from app.data.session import get_session
from app.infra.rabbitmq import current_lane
from app.runtime.data import Data
from app.runtime.emit import emit
from app.runtime.placement import DEFAULT_APP

logger = logging.getLogger(__name__)


def _current_app() -> str:
    return os.getenv("APP_NAME") or DEFAULT_APP


def deserialize_data(data_type: str, payload_json: dict[str, Any]) -> Data:
    """Resolve a fully-qualified `module.Class` name to its Data subclass
    and reconstruct an instance from the JSON payload.
    """
    mod_name, _, cls_name = data_type.rpartition(".")
    if not mod_name:
        raise RuntimeError(f"invalid data_type {data_type!r}")
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    if not issubclass(cls, Data):
        raise RuntimeError(f"{data_type!r} resolved to non-Data class")
    return cls(**payload_json)


class _Bound:
    """Async context manager: bind trace_id + lane vars from a row."""

    def __init__(self, *, trace_id: str | None, lane: str | None) -> None:
        self._t_token = None
        self._l_token = None
        self._tid = trace_id
        self._lane = lane

    async def __aenter__(self):
        self._t_token = trace_id_var.set(self._tid)
        self._l_token = lane_var.set(self._lane)
        return self

    async def __aexit__(self, *exc):
        lane_var.reset(self._l_token)
        trace_id_var.reset(self._t_token)


def bind_propagation_from_payload(*, trace_id: str | None,
                                  lane: str | None) -> _Bound:
    return _Bound(trace_id=trace_id, lane=lane)


async def _drain_once(*, app: str, lane: str | None,
                      batch_size: int = 32) -> int:
    """One pass of the loop. Returns number of rows touched."""
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT id, data_type, payload_json, lane, trace_id, origin_app "
            "FROM runtime_outbox "
            "WHERE state = 'pending' "
            "  AND next_attempt_at <= now() "
            "  AND origin_app = :app "
            "  AND lane IS NOT DISTINCT FROM :lane "
            "ORDER BY id "
            "LIMIT :n "
            "FOR UPDATE SKIP LOCKED"
        ), {"n": batch_size, "app": app, "lane": lane})).mappings().all()

        for row in rows:
            try:
                async with bind_propagation_from_payload(
                    trace_id=row["trace_id"], lane=row["lane"],
                ):
                    data = deserialize_data(row["data_type"], row["payload_json"])
                    await emit(data)
                await s.execute(text(
                    "UPDATE runtime_outbox "
                    "SET state='dispatched', dispatched_at=now() "
                    "WHERE id=:i"
                ), {"i": row["id"]})
            except Exception as exc:
                logger.exception(
                    "outbox dispatch failed id=%s data_type=%s",
                    row["id"], row["data_type"],
                )
                await s.execute(text(
                    "UPDATE runtime_outbox "
                    "SET attempts = attempts + 1, "
                    "    last_error = :e, "
                    "    next_attempt_at = now() + (interval '5 seconds' * power(2, attempts)) "
                    "WHERE id=:i"
                ), {"i": row["id"], "e": str(exc)[:500]})
        await s.commit()
        return len(rows)


async def dispatcher_loop(*, batch_size: int = 32, idle_sleep_ms: int = 200) -> None:
    """Long-running loop. Cancel the task to stop."""
    app = _current_app()
    lane = current_lane()
    logger.info("outbox dispatcher started app=%s lane=%s", app, lane)
    try:
        while True:
            n = await _drain_once(app=app, lane=lane, batch_size=batch_size)
            if n == 0:
                await asyncio.sleep(idle_sleep_ms / 1000)
    except asyncio.CancelledError:
        logger.info("outbox dispatcher stopping")
        raise
```

- [ ] **Step 9.4: Verify dispatcher tests pass**

Run: `uv run pytest apps/agent-service/tests/runtime/test_outbox_dispatcher.py -v`
Expected: 6 passed.

- [ ] **Step 9.5: Wire into Runtime.run() (entry 1 of 2)**

Modify `apps/agent-service/app/runtime/runtime.py` (or `runtime/engine.py` — search for `Runtime.run` definition):

```python
import asyncio
from app.runtime.outbox_dispatcher import dispatcher_loop

class Runtime:
    ...
    async def run(self):
        ...  # existing setup
        self._outbox_dispatcher_task = asyncio.create_task(dispatcher_loop())
        ...

    async def stop(self):
        if getattr(self, "_outbox_dispatcher_task", None):
            self._outbox_dispatcher_task.cancel()
            try:
                await self._outbox_dispatcher_task
            except asyncio.CancelledError:
                pass
        ...
```

- [ ] **Step 9.6: Wire into FastAPI lifespan (entry 2 of 2)**

Modify `apps/agent-service/app/main.py` (or wherever `lifespan` is defined):

```python
from app.runtime.outbox_dispatcher import dispatcher_loop

@asynccontextmanager
async def lifespan(app):
    ...  # existing startup
    outbox_task = asyncio.create_task(dispatcher_loop())
    try:
        yield
    finally:
        outbox_task.cancel()
        try:
            await outbox_task
        except asyncio.CancelledError:
            pass
        ...  # existing shutdown
```

> **Why both** (per `feedback_main_vs_runtime_run_dual_entry`): `Runtime.run` is used by worker processes; `lifespan` is used by the agent-service HTTP main process. Either alone misses one of the two entry points.

- [ ] **Step 9.7: Smoke test — boot the runtime locally, seed a row, observe it drained**

Run (in a scratch terminal — DO NOT commit any output):
```bash
APP_NAME=agent-service uv run python -c '
import anyio
from sqlalchemy import text
from app.data.session import get_session
from app.runtime.outbox import RUNTIME_OUTBOX_DDL
from app.runtime.outbox_dispatcher import _drain_once

async def main():
    async with get_session() as s:
        for ddl in RUNTIME_OUTBOX_DDL: await s.execute(text(ddl))
        await s.execute(text("INSERT INTO runtime_outbox (data_type, payload_json, origin_app) VALUES (:d, :p::jsonb, :a)"), {"d":"app.runtime.data.Data", "p":"{}", "a":"agent-service"})
        await s.commit()
    n = await _drain_once(app="agent-service", lane=None)
    print(f"drained {n}")
anyio.run(main)
'
```
Expected: `drained 1` (or higher if other rows were already pending). The test exists to confirm DB connectivity in dev — don't regard it as a contract.

- [ ] **Step 9.8: Lint**

Run: `uv run ruff check apps/agent-service/app/runtime/outbox_dispatcher.py apps/agent-service/app/runtime/runtime.py apps/agent-service/app/main.py apps/agent-service/tests/runtime/test_outbox_dispatcher.py`

- [ ] **Step 9.9: Commit**

```bash
git add apps/agent-service/app/runtime/outbox_dispatcher.py \
        apps/agent-service/app/runtime/runtime.py \
        apps/agent-service/app/main.py \
        apps/agent-service/tests/runtime/test_outbox_dispatcher.py
git commit -m "$(cat <<'EOF'
feat(runtime): outbox dispatcher_loop (calls emit) + lifespan dual entry

Phase 7b Gap 8 step 2/3. The dispatcher SELECTs pending rows filtered
by (origin_app, lane) (NULL-safe), reconstructs the Data instance,
binds propagation from the row, and calls emit(data) — letting emit
own the wire fan-out (in-process / durable / debounce / sink). Started
from both Runtime.run() (worker entry) and main.py lifespan (HTTP
entry); see project memory feedback_main_vs_runtime_run_dual_entry.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10 — Gap 8: migrate 8 mutation nodes to `transactional_emit`

**Goal:** Cut every category-A mutation node from "commit-then-emit" to in-transaction outbox append. Per spec §4.5.1 — exactly 8 sites, each gets one focused diff.

**Files (one diff per file; commit groups OK but keep each file's edits self-contained):**
- `apps/agent-service/app/agent/tools/commit_abstract.py:64`
- `apps/agent-service/app/agent/tools/notes.py:42`
- `apps/agent-service/app/agent/tools/update_schedule.py:46`
- `apps/agent-service/app/life/proactive.py:148, 150`
- `apps/agent-service/app/life/tool.py:104`
- `apps/agent-service/app/life/glimpse.py:242`
- `apps/agent-service/app/nodes/memory_pipelines.py:203`

**Migration pattern** (applied to all 8 sites):

The current pattern at every site is:

```python
async with get_session() as s:
    await <mutation>(s, ...)
await emit(<EventData>(...))   # outside the session block — "commit-then-emit"
```

Replace with:

```python
from app.runtime import transactional_emit

async with get_session() as s:
    await <mutation>(s, ...)
    async with transactional_emit(s) as emitter:
        await emitter.append(<EventData>(...))
# get_session() context-exit commits both the mutation row and the outbox row atomically
```

For `life/proactive.py:148, 150` (two emits in the same block), append both to the same emitter:

```python
async with transactional_emit(s) as emitter:
    await emitter.append(Message.from_cm(msg))
    await emitter.append(ChatTrigger(...))
```

For `agent/tools/update_schedule.py`, also delete the `# emit AFTER commit (Gap 8 spec convention)` comment block above the emit call.

**Concrete site list** — each site gets one Step 10.S.x sub-step:

| # | File | Line | Mutation | Event Data |
|---|---|---|---|---|
| S.1 | `apps/agent-service/app/agent/tools/commit_abstract.py` | 64 | `insert_abstract_memory + insert_memory_edge` | `AbstractMemoryCommitted` |
| S.2 | `apps/agent-service/app/agent/tools/notes.py` | 42 | `insert_note` | `NoteCreated` |
| S.3 | `apps/agent-service/app/agent/tools/update_schedule.py` | 46 | `insert_schedule_revision` | `ScheduleRevisionCreated` |
| S.4 | `apps/agent-service/app/life/proactive.py` | 148 + 150 | proactive message INSERT | `Message` + `ChatTrigger` |
| S.5 | `apps/agent-service/app/life/tool.py` | 104 | `insert_life_state` | `LifeStateChanged` |
| S.6 | `apps/agent-service/app/life/glimpse.py` | 242 | `Q.insert_fragment` | `MemoryFragmentRequest` |
| S.7 | `apps/agent-service/app/nodes/memory_pipelines.py` | 203 | `insert_fragment` (afterthought) | `MemoryFragmentRequest` |

(Note: §4.5.1 lists 8 — S.4 counts as 2 sites within one file/block; treated as one migration unit here.)

- [ ] **Step 10.1: Add a single migration regression test file**

Create `apps/agent-service/tests/runtime/test_outbox_migration.py`. One test per site asserting the row appears after the function runs and the rollback property holds. Copy fixtures from each module's existing tests (`grep -rn "<module name>" apps/agent-service/tests/`):

```python
"""Phase 7b Gap 8: assert each migrated mutation node writes an outbox row
inside its business transaction and rolls back together on failure."""
import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.outbox import RUNTIME_OUTBOX_DDL


@pytest.fixture(autouse=True)
async def _setup():
    async with get_session() as s:
        for ddl in RUNTIME_OUTBOX_DDL:
            await s.execute(text(ddl))
        await s.execute(text("DELETE FROM runtime_outbox"))
        await s.commit()


async def _outbox_data_types() -> list[str]:
    async with get_session() as s:
        return (await s.execute(text(
            "SELECT data_type FROM runtime_outbox ORDER BY id"
        ))).scalars().all()


# S.1 commit_abstract -------------------------------------------------------
@pytest.mark.asyncio
async def test_commit_abstract_writes_outbox_row():
    from app.agent.tools.commit_abstract import commit_abstract
    # If commit_abstract has heavy external deps, copy mock setup from the
    # existing tests/agent/tools/test_commit_abstract.py file.
    await commit_abstract(persona_id="p1", abstract_text="hello",
                          supported_by_fact_ids=[])
    types = await _outbox_data_types()
    assert any("AbstractMemoryCommitted" in t for t in types)


# S.2 notes -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_notes_create_writes_outbox_row():
    from app.agent.tools.notes import create_note  # adjust to actual entry
    await create_note(persona_id="p1", content="n1")
    types = await _outbox_data_types()
    assert any("NoteCreated" in t for t in types)


# S.3 update_schedule -------------------------------------------------------
@pytest.mark.asyncio
async def test_update_schedule_writes_outbox_row():
    from app.agent.tools.update_schedule import _update_schedule_impl
    await _update_schedule_impl(persona_id="p1",  # adjust to actual signature
                                 schedule_data={})
    types = await _outbox_data_types()
    assert any("ScheduleRevisionCreated" in t for t in types)


# S.4 proactive (two emits in one block) -----------------------------------
@pytest.mark.asyncio
async def test_proactive_writes_two_outbox_rows():
    from app.life.proactive import _persist_proactive  # adjust to actual entry
    await _persist_proactive(persona_id="p1", chat_id="c1", content="hi")
    types = await _outbox_data_types()
    assert sum(1 for t in types if "Message" in t) == 1
    assert sum(1 for t in types if "ChatTrigger" in t) == 1


# S.5 life/tool -------------------------------------------------------------
@pytest.mark.asyncio
async def test_life_tool_writes_outbox_row():
    from app.life.tool import update_life_state  # adjust to actual entry
    await update_life_state(persona_id="p1", state_data={})
    types = await _outbox_data_types()
    assert any("LifeStateChanged" in t for t in types)


# S.6 glimpse ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_glimpse_writes_outbox_row():
    from app.life.glimpse import run_glimpse  # adjust to actual entry
    # glimpse path conditionally emits; test must hit a path that does
    await run_glimpse(persona_id="p1", chat_id="c1")
    types = await _outbox_data_types()
    assert any("MemoryFragmentRequest" in t for t in types)


# S.7 memory_pipelines (afterthought) ---------------------------------------
@pytest.mark.asyncio
async def test_afterthought_writes_outbox_row():
    from app.nodes.memory_pipelines import _create_afterthought_fragment
    await _create_afterthought_fragment(chat_id="c1", persona_id="p1",
                                        content="x")
    types = await _outbox_data_types()
    assert any("MemoryFragmentRequest" in t for t in types)


# Rollback contract — uses S.1 as exemplar ---------------------------------
@pytest.mark.asyncio
async def test_rollback_drops_outbox_row():
    """If the surrounding session block raises, the outbox row must rollback."""
    from app.runtime.outbox import transactional_emit
    from app.domain.agent_tool_events import AbstractMemoryCommitted

    try:
        async with get_session() as s:
            async with transactional_emit(s) as emitter:
                await emitter.append(AbstractMemoryCommitted(abstract_id="x"))
            raise RuntimeError("simulate downstream failure before commit")
    except RuntimeError:
        pass
    types = await _outbox_data_types()
    assert types == []
```

> **Test signature gaps:** The `<actual entry>` placeholders mean "look up the public entry point of each module". For each S.N test, before running it the first time, replace the import with the real entry function from that module — the actual signatures live next to the line numbers given in the table.

- [ ] **Step 10.2: Run the test file once to confirm everything fails (red phase)**

Run: `uv run pytest apps/agent-service/tests/runtime/test_outbox_migration.py -v`
Expected: 7 site tests FAIL (outbox empty); rollback test PASSES (transactional_emit already shipped in Task 8). After the migration steps below, all 8 should pass.

- [ ] **Step 10.3: Migrate S.1 — `agent/tools/commit_abstract.py:64`**

In `apps/agent-service/app/agent/tools/commit_abstract.py`:
1. Add `from app.runtime import transactional_emit` to the imports.
2. Move `await emit(AbstractMemoryCommitted(...))` from outside the `async with get_session()` block to inside, wrapped by `async with transactional_emit(s) as emitter: await emitter.append(...)`.

Run: `uv run pytest apps/agent-service/tests/runtime/test_outbox_migration.py::test_commit_abstract_writes_outbox_row -v`
Expected: PASS.

- [ ] **Step 10.4: Migrate S.2 — `agent/tools/notes.py:42`**

Same pattern. Verify: `uv run pytest apps/agent-service/tests/runtime/test_outbox_migration.py::test_notes_create_writes_outbox_row -v` → PASS.

- [ ] **Step 10.5: Migrate S.3 — `agent/tools/update_schedule.py:46`**

Same pattern + delete the `# emit AFTER commit (Gap 8 spec convention)` comment block. Verify: `uv run pytest apps/agent-service/tests/runtime/test_outbox_migration.py::test_update_schedule_writes_outbox_row -v` → PASS.

- [ ] **Step 10.6: Migrate S.4 — `life/proactive.py:148, 150`**

Two appends in the same emitter (see migration pattern at top of Task 10). Verify: `uv run pytest apps/agent-service/tests/runtime/test_outbox_migration.py::test_proactive_writes_two_outbox_rows -v` → PASS.

- [ ] **Step 10.7: Migrate S.5 — `life/tool.py:104`**

Same pattern. Verify: `uv run pytest apps/agent-service/tests/runtime/test_outbox_migration.py::test_life_tool_writes_outbox_row -v` → PASS.

- [ ] **Step 10.8: Migrate S.6 — `life/glimpse.py:242`**

Same pattern. The glimpse path conditionally emits; ensure the test fixture hits a path where `observation` is non-empty (see source line 231). Verify: `uv run pytest apps/agent-service/tests/runtime/test_outbox_migration.py::test_glimpse_writes_outbox_row -v` → PASS.

- [ ] **Step 10.9: Migrate S.7 — `nodes/memory_pipelines.py:203`**

Same pattern. Verify: `uv run pytest apps/agent-service/tests/runtime/test_outbox_migration.py::test_afterthought_writes_outbox_row -v` → PASS.

After all 7 site migrations:

- [ ] **Step 10.10: Run full agent-service test suite**

Run: `uv run pytest apps/agent-service/tests/ -v`
Expected: all green. Existing tests for the migrated functions may break if they assert direct `emit` calls — update them to assert on the outbox row instead, or to use `_drain_once` to fire the emit.

- [ ] **Step 10.11: Run the count gate locally**

Run: `grep -rn '\bawait emit(' apps/agent-service/app/{nodes,agent,chat,life,memory,long_tasks}/ --include='*.py' | wc -l`
Expected: `14` (per spec §4.5.4 — 12 category-B + 2 category-C docstring).

- [ ] **Step 10.12: Lint the changed files**

Run: `uv run ruff check $(git diff --name-only --diff-filter=AM HEAD | grep '\.py$')`

- [ ] **Step 10.13: Commit**

```bash
git add apps/agent-service/app/agent/tools/commit_abstract.py \
        apps/agent-service/app/agent/tools/notes.py \
        apps/agent-service/app/agent/tools/update_schedule.py \
        apps/agent-service/app/life/proactive.py \
        apps/agent-service/app/life/tool.py \
        apps/agent-service/app/life/glimpse.py \
        apps/agent-service/app/nodes/memory_pipelines.py \
        apps/agent-service/tests/runtime/test_outbox_migration_*.py
git commit -m "$(cat <<'EOF'
refactor(business): migrate 8 mutation nodes to transactional_emit

Phase 7b Gap 8 step 3/3. Each site now appends to runtime_outbox
inside the same `async with get_session()` block that performs the
business INSERT/UPDATE; the dispatcher fires emit() once the session
commit makes the outbox row visible. Removes the "emit AFTER commit"
comment from update_schedule (was load-bearing under the old contract,
now redundant). Per-site outbox-write + rollback regression tests
guard the contract.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11 — CI: close Gap 8 / 12 / 18 grep gate

**Goal:** Promote Gap 8/12/18 from "open baseline" to "closed exact-zero" in the CI gate.

**Files:**
- Modify: `.github/workflows/grep-gate.yml`

- [ ] **Step 11.1: Read current gate file**

Run: `cat .github/workflows/grep-gate.yml` to see the existing layout (closed-gap-zero job vs baseline job).

- [ ] **Step 11.2: Add closed-gap-zero checks for Gap 8 / 12 / 18**

Modify `.github/workflows/grep-gate.yml`. Inside the closed-gap-zero job (or create one if absent), add steps:

```yaml
      - name: Gap 8 — business-area await emit count == 14
        run: |
          COUNT=$(grep -rn '\bawait emit(' \
            apps/agent-service/app/{nodes,agent,chat,life,memory,long_tasks}/ \
            --include='*.py' | wc -l)
          echo "await emit count: $COUNT (expected 14)"
          test "$COUNT" = "14"

      - name: Gap 8 — no commit-then-emit comments
        run: |
          ! grep -rn "# emit AFTER commit\|# commit-then-emit" \
            apps/agent-service/app/

      - name: Gap 12 — Makefile DLQ targets and runbook present
        run: |
          grep -q "^dlq-inspect:" Makefile
          grep -q "^dlq-replay:" Makefile
          grep -q "^dlq-dry-run:" Makefile
          test -f docs/runbooks/dlq-replay.md

      - name: Gap 18 — no don't-catch comments / requeue=False / nack literals in business
        run: |
          ! grep -rn "# 不要 catch\|# 不要 try/except\|# don't catch\|requeue=False\|nack" \
            apps/agent-service/app/{nodes,agent,chat,life,memory}/
```

- [ ] **Step 11.3: Run the gates locally**

Run each gate's commands manually:
```bash
COUNT=$(grep -rn '\bawait emit(' \
  apps/agent-service/app/{nodes,agent,chat,life,memory,long_tasks}/ \
  --include='*.py' | wc -l)
echo $COUNT
# expect 14
grep -rn "# emit AFTER commit\|# commit-then-emit" apps/agent-service/app/ ; echo $?
# expect: no matches; exit 1
grep -q "^dlq-inspect:" Makefile && grep -q "^dlq-replay:" Makefile && grep -q "^dlq-dry-run:" Makefile && test -f docs/runbooks/dlq-replay.md && echo OK
# expect: OK
grep -rn "# 不要 catch\|# 不要 try/except\|# don't catch\|requeue=False\|nack" \
  apps/agent-service/app/{nodes,agent,chat,life,memory}/ ; echo $?
# expect: no matches; exit 1
```
All four must pass before commit.

- [ ] **Step 11.4: Commit**

```bash
git add .github/workflows/grep-gate.yml
git commit -m "$(cat <<'EOF'
chore(ci): close Gap 8 / 12 / 18 in grep gate

Phase 7b CI promotion. Gap 8 enforces business-area `await emit(`
count == 14 (spec §4.5.4: 12 category-B + 2 docstring); plus a
zero-tolerance grep for the legacy "emit AFTER commit" comments.
Gap 12 verifies Makefile targets and runbook exist. Gap 18 keeps
business-area free of "don't catch" comments, requeue=False, and
nack literals (only allowed in runtime/durable.py).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification (run all four)

- [ ] **F.1: Full test suite green**

Run: `uv run pytest apps/agent-service/ -v`
Expected: all green; no skips for Phase 7b tests.

- [ ] **F.2: Lint green**

Run: `uv run ruff check apps/agent-service/`
Expected: no warnings.

- [ ] **F.3: Type check** (if mypy is configured)

Run: `uv run mypy apps/agent-service/app/runtime/outbox.py apps/agent-service/app/runtime/outbox_dispatcher.py apps/agent-service/app/runtime/errors.py apps/agent-service/app/runtime/review_queue.py apps/agent-service/app/nodes/dlq_admin.py`
Expected: no errors.

- [ ] **F.4: Branch ready for `/deploy-test` or PR**

Run:
```bash
git log --oneline main..HEAD
git diff --stat main..HEAD
```
Expected: 12 commits (1 spec + 11 task commits); diff totals roughly +1500/-300 (per spec §3 estimate, slightly over due to round-2/3/4 reviewer-driven additions).

---

## Notes for the executing engineer

- **Schema lives in DDL lists.** Do NOT introduce alembic. Boot path runs each `RUNTIME_*_DDL` list once at startup; `CREATE ... IF NOT EXISTS` makes them idempotent.
- **Lane normalization is non-negotiable.** Always use `from app.infra.rabbitmq import current_lane`. Never read `lane_var.get()` directly outside `infra/` and `runtime/propagation.py`.
- **Helper contract for `_route_consumer_exception` (Task 2):** `return` = handled (caller's `process(__aexit__)` acks); `raise original exc` = DLQ (caller's `process(requeue=False)` nacks). Helper NEVER calls `message.ack()` / `message.nack()` itself.
- **6-step requeue order (Task 6):** audit-cleared → delete_inflight → publish_with_confirm → audit-requeued → ack. Skipping any step or reordering breaks the failure-recovery story (see runbook).
- **Dispatcher only ever calls `emit(data)`.** Do NOT call `mq.publish_with_confirm` from the dispatcher — emit() owns wire fan-out (in-process / durable / debounce / sink).
- **Migration count gate (Task 11):** if you legitimately add a new pure-transform site, register it in spec §4.5.2 and update the gate's expected count in the same PR.
- **Project rules to respect:**
  - `CLAUDE.md`: no English-only PR titles policy applies to **squash commit / PR title**, not in-progress task commits. Task commits in this plan are written in English already, but Chinese is fine for in-progress commits if you prefer.
  - Memory `feedback_no_oneoff_scripts_in_repo`: do not add `scripts/dlq_cli.py` etc. — admin endpoints + Makefile is the design.
  - Memory `feedback_aio_pika_process_context_double_ack`: do not call `message.ack()` / `message.nack()` while inside `async with message.process(...)`.
