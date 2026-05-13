"""B7: declarative per-key fan-out at the wire layer.

Replaces hand-rolled ``_fan_out_per_persona`` loops inside @node bodies:
the wire itself declares "at emit time, call the extractor, then emit
one copy per key with that key injected as attributes". Failure
isolation is part of the contract — one key's consumer raising MUST
NOT abort the other keys.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.graph import GraphError, compile_graph
from app.runtime.node import node
from app.runtime.wire import WIRING_REGISTRY, clear_wiring, wire


class Tick(Data):
    ts: Annotated[str, Key]
    persona_id: str = ""

    class Meta:
        transient = True


class PersistedTick(Data):
    """Non-transient variant for the durable+fan_out_per combo test."""
    ts: Annotated[str, Key]
    persona_id: str = ""


class State(Data):
    """As-latest target for the with_latest combo test."""
    persona_id: Annotated[str, Key]
    v: int = 0


def setup_function():
    clear_wiring()
    reset_emit_runtime()


# ---------------------------------------------------------------------------
# DSL: fan_out_per writes the extractor onto the WireSpec
# ---------------------------------------------------------------------------


def test_fan_out_per_records_extractor_on_spec():
    """``.fan_out_per(extractor)`` stores the callable on the WireSpec."""

    async def _keys() -> list[dict]:
        return [{"persona_id": "a"}, {"persona_id": "b"}]

    @node
    async def consumer(t: Tick) -> None: ...

    wire(Tick).fan_out_per(_keys).to(consumer)
    spec = WIRING_REGISTRY[0]
    assert spec.fan_out_extractor is _keys


def test_fan_out_per_rejects_non_callable():
    """Non-callable extractor must raise at DSL construction time."""

    @node
    async def consumer(t: Tick) -> None: ...

    with pytest.raises(TypeError, match="fan_out_per.*callable"):
        wire(Tick).fan_out_per([{"persona_id": "a"}]).to(consumer)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Compile-time validation: fan_out_per cannot combine with durable/debounce/with_latest
# ---------------------------------------------------------------------------


def test_fan_out_per_with_durable_rejected():
    """``.fan_out_per().durable()`` is rejected at compile time."""

    async def _keys() -> list[dict]:
        return [{"persona_id": "a"}]

    @node
    async def c(t: PersistedTick) -> None: ...

    wire(PersistedTick).fan_out_per(_keys).to(c).durable()
    with pytest.raises(GraphError, match="fan_out_per.*durable"):
        compile_graph()


def test_fan_out_per_with_debounce_rejected():
    """``.fan_out_per().debounce()`` is rejected at compile time."""

    async def _keys() -> list[dict]:
        return [{"persona_id": "a"}]

    @node
    async def c(t: Tick) -> None: ...

    wire(Tick).fan_out_per(_keys).to(c).debounce(
        seconds=1, max_buffer=1, key_by=lambda t: t.ts
    )
    with pytest.raises(GraphError, match="fan_out_per.*debounce"):
        compile_graph()


def test_fan_out_per_with_with_latest_rejected():
    """``.fan_out_per().with_latest(X)`` is rejected at compile time.

    with_latest joins on the latest target's first Key, but fan_out_per
    mutates the primary Data's fields — the resolution semantics get
    ambiguous. Refuse the combination until someone proves it needed.
    """

    async def _keys() -> list[dict]:
        return [{"persona_id": "a"}]

    @node
    async def c(t: Tick, s: State) -> None: ...

    # State must have an as_latest wire so the with_latest check passes
    # the other validation gates first.
    @node
    async def sink(s: State) -> None: ...

    wire(State).to(sink).as_latest()
    # Tick needs `persona_id` to be its first Key so with_latest(State)
    # passes the join-key validation (4e). Tick declares persona_id as
    # a non-Key field; instead the test uses State's persona_id as join
    # key — which means Tick must expose persona_id at the attribute
    # level (it does, default ""). For the join-key field validation
    # in compile (4e) we only need Tick to declare ``persona_id`` as a
    # model field — passes.
    wire(Tick).fan_out_per(_keys).to(c).with_latest(State)
    with pytest.raises(GraphError, match="fan_out_per.*with_latest"):
        compile_graph()


# ---------------------------------------------------------------------------
# Emit: extractor is called, one copy per key, with key injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_per_emits_one_copy_per_key_with_inject():
    """emit calls extractor; consumer fires once per key with injected fields."""
    seen: list[Tick] = []

    @node
    async def consumer(t: Tick) -> None:
        seen.append(t)

    async def _keys() -> list[dict]:
        return [{"persona_id": "alice"}, {"persona_id": "bob"}, {"persona_id": "carol"}]

    wire(Tick).fan_out_per(_keys).to(consumer)
    compile_graph()
    await emit(Tick(ts="2026-05-13T10:00:00"))

    assert len(seen) == 3
    assert {t.persona_id for t in seen} == {"alice", "bob", "carol"}
    assert all(t.ts == "2026-05-13T10:00:00" for t in seen)


@pytest.mark.asyncio
async def test_fan_out_per_sync_extractor_supported():
    """Sync extractor (not coroutine) should also be supported."""
    seen: list[str] = []

    @node
    async def consumer(t: Tick) -> None:
        seen.append(t.persona_id)

    def _keys() -> list[dict]:
        return [{"persona_id": "x"}, {"persona_id": "y"}]

    wire(Tick).fan_out_per(_keys).to(consumer)
    compile_graph()
    await emit(Tick(ts="t"))

    assert set(seen) == {"x", "y"}


@pytest.mark.asyncio
async def test_fan_out_per_empty_extractor_emits_nothing():
    """Empty key list → consumer not called, no exception raised."""
    seen: list[Tick] = []

    @node
    async def consumer(t: Tick) -> None:
        seen.append(t)

    async def _keys() -> list[dict]:
        return []

    wire(Tick).fan_out_per(_keys).to(consumer)
    compile_graph()
    await emit(Tick(ts="t"))

    assert seen == []


# ---------------------------------------------------------------------------
# Critical contract: failure isolation between fanned-out keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_per_isolates_consumer_failure_per_key():
    """One key's consumer raising MUST NOT abort the others.

    This is the core guarantee the framework owes business code so it
    can drop the hand-rolled try/except loop. The drill in the plan
    description ("某 persona 失败不影响其他 persona") is this assertion.
    """
    succeeded: list[str] = []

    @node
    async def flaky(t: Tick) -> None:
        if t.persona_id == "bad":
            raise RuntimeError("simulated per-persona failure")
        succeeded.append(t.persona_id)

    async def _keys() -> list[dict]:
        return [
            {"persona_id": "a"},
            {"persona_id": "bad"},  # this one raises
            {"persona_id": "b"},
            {"persona_id": "c"},
        ]

    wire(Tick).fan_out_per(_keys).to(flaky)
    compile_graph()
    # emit must NOT raise — failure isolation is the whole point
    await emit(Tick(ts="t"))

    # all non-bad keys succeeded
    assert set(succeeded) == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_fan_out_per_extractor_failure_is_logged_not_raised(caplog):
    """Extractor raising → emit logs + returns (one tick lost, next recovers).

    Same fail-soft semantic as ``_fan_out_per_persona``'s try/except
    around ``_list_persona_ids``: DB jitter on key listing can't be
    allowed to bubble back to the source loop (``_record_source_error``
    would crash the process).
    """
    import logging

    @node
    async def consumer(t: Tick) -> None: ...

    async def _broken() -> list[dict]:
        raise RuntimeError("simulated DB jitter")

    wire(Tick).fan_out_per(_broken).to(consumer)
    compile_graph()
    with caplog.at_level(logging.WARNING):
        await emit(Tick(ts="t"))  # must not raise

    assert any("fan_out_per" in r.message or "fan_out" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_fan_out_per_multi_consumer_isolated():
    """Multi-consumer fan_out_per: a failure in consumer1[key1] must not
    block consumer1[key2] or consumer2[key1].
    """
    succeeded: list[tuple[str, str]] = []

    @node
    async def c1(t: Tick) -> None:
        if t.persona_id == "bad":
            raise RuntimeError("c1 failed on bad")
        succeeded.append(("c1", t.persona_id))

    @node
    async def c2(t: Tick) -> None:
        succeeded.append(("c2", t.persona_id))

    async def _keys() -> list[dict]:
        return [{"persona_id": "a"}, {"persona_id": "bad"}, {"persona_id": "b"}]

    wire(Tick).fan_out_per(_keys).to(c1, c2)
    compile_graph()
    await emit(Tick(ts="t"))

    # c1 succeeded on a, b (skipped bad); c2 succeeded on all 3
    assert ("c1", "a") in succeeded
    assert ("c1", "b") in succeeded
    assert ("c1", "bad") not in succeeded
    assert ("c2", "a") in succeeded
    assert ("c2", "bad") in succeeded
    assert ("c2", "b") in succeeded
