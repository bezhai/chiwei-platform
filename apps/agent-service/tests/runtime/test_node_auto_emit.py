"""B8: systematic coverage of `@node` wrapper auto-emit behavior.

These tests exercise the wrapper installed at ``node.py:92-99``:

- N6  : returning a ``Data`` instance auto-emits it into the graph
- N7  : returning ``None`` skips emission
- N6 boundary: the wrapper still returns the value to its caller so unit
  tests can assert on it
- N6 anti-pattern: manual ``await emit(returned_data)`` followed by
  ``return same_data`` produces a duplicate emit. Contract §1 forbids
  this writing pattern, but the framework deliberately does **not**
  enforce it at runtime — this test pins the observed behavior so the
  anti-pattern can't silently become a runtime guard.
- Multi-output fan-out: manual emit of a *different* Data + return
  another Data is the legal multi-output pattern (life_dataflow
  per-persona fan-out). Each emit triggers its own consumer once.
- ``Data | None`` union return type with both branches.
- Plain async functions without ``@node`` do not register and therefore
  never trigger auto-emit (the wrapper is what performs the emit).

Contract reference: docs/guides/dataflow-node-contract.md §1 (N1-N8) and
the "N6 自动 emit 边界" paragraph.
"""

from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.graph import compile_graph
from app.runtime.node import NODE_REGISTRY, node
from app.runtime.wire import clear_wiring, wire


class M(Data):
    mid: Annotated[str, Key]
    text: str = ""


class Frag(Data):
    fid: Annotated[str, Key]
    vec: list[float] = []


class Other(Data):
    oid: Annotated[str, Key]
    payload: str = ""


# Per-test recorder. setup_function clears it.
calls: list = []


def setup_function():
    clear_wiring()
    calls.clear()
    reset_emit_runtime()


# ---------------------------------------------------------------------------
# N6: returning Data auto-emits to downstream consumer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returning_data_auto_emits_to_consumer():
    """N6: ``return Frag(...)`` triggers ``emit(Frag)`` once via wrapper."""

    @node
    async def consumer(f: Frag) -> None:
        calls.append(("consumer", f.fid))

    @node
    async def producer(m: M) -> Frag:
        return Frag(fid="f1")

    wire(M).to(producer)
    wire(Frag).to(consumer)
    compile_graph()

    await emit(M(mid="m1"))

    assert calls == [("consumer", "f1")]


# ---------------------------------------------------------------------------
# N7: returning None skips emit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returning_none_skips_emit():
    """N7: returning ``None`` from a ``Data | None`` node skips downstream."""

    @node
    async def consumer(f: Frag) -> None:
        calls.append(("consumer", f.fid))

    @node
    async def producer(m: M) -> Frag | None:
        return None

    wire(M).to(producer)
    wire(Frag).to(consumer)
    compile_graph()

    await emit(M(mid="m1"))

    assert calls == []


# ---------------------------------------------------------------------------
# N6 boundary: wrapper still returns the value to the caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapper_returns_value_to_caller():
    """The wrapper emits *and* returns the Data instance, so unit tests
    (and any rare direct callers) can still assert on the value. The
    contract documents this explicitly in the "N6 自动 emit 边界" line.
    """

    @node
    async def producer(m: M) -> Frag:
        return Frag(fid="kept")

    # No downstream wire for Frag — emit is a no-op, but the return
    # value must still surface.
    wire(M).to(producer)
    compile_graph()

    # Call the wrapper directly (mimic a unit test or in-process caller).
    result = await producer(m=M(mid="m1"))

    assert isinstance(result, Frag)
    assert result.fid == "kept"


@pytest.mark.asyncio
async def test_wrapper_returns_none_when_node_returns_none():
    """Mirror of above: ``return None`` also surfaces to the caller as
    ``None`` (the wrapper doesn't replace the return with a sentinel)."""

    @node
    async def producer(m: M) -> Frag | None:
        return None

    wire(M).to(producer)
    compile_graph()

    result = await producer(m=M(mid="m1"))

    assert result is None


# ---------------------------------------------------------------------------
# N6 anti-pattern: manual emit + return same Data = DUPLICATE emit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_emit_then_return_same_data_double_emits():
    """Contract §1 forbids ``await emit(x); return x`` (duplicate emit),
    but the framework intentionally does **not** enforce this at runtime
    — adding a guard would block legitimate fan-out patterns that emit
    different Data and then return one of them.

    This test pins the resulting behavior: the consumer fires twice. If
    a future change decides to enforce N6 at runtime, this test will
    fail and the contract paragraph must be updated in lock-step.
    """

    @node
    async def consumer(f: Frag) -> None:
        calls.append(("consumer", f.fid))

    @node
    async def producer(m: M) -> Frag:
        same = Frag(fid="dup")
        await emit(same)  # manual emit (contract forbids this writing pattern)
        return same       # wrapper auto-emits again

    wire(M).to(producer)
    wire(Frag).to(consumer)
    compile_graph()

    await emit(M(mid="m1"))

    # Two consumer invocations — proof of the duplicate-emit footgun.
    assert calls == [("consumer", "dup"), ("consumer", "dup")]


# ---------------------------------------------------------------------------
# Legitimate multi-output: manual emit of a DIFFERENT Data + return another
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_emit_other_data_plus_return_is_legal_fan_out():
    """The reason the framework can't blanket-ban manual ``emit`` is the
    legitimate multi-output pattern: a node produces several Data types,
    emits them individually, and returns one of them (or another). Each
    emit reaches its own consumer exactly once; the returned Data is
    auto-emitted by the wrapper exactly once. This mirrors
    ``life_dataflow._fan_out_per_persona`` and similar real nodes.
    """

    @node
    async def other_consumer(o: Other) -> None:
        calls.append(("other", o.oid))

    @node
    async def frag_consumer(f: Frag) -> None:
        calls.append(("frag", f.fid))

    @node
    async def producer(m: M) -> Frag:
        await emit(Other(oid="o1"))
        return Frag(fid="f1")

    wire(M).to(producer)
    wire(Other).to(other_consumer)
    wire(Frag).to(frag_consumer)
    compile_graph()

    await emit(M(mid="m1"))

    # Order-insensitive: manual emit fires synchronously inside the
    # producer, then the wrapper emits the returned Frag after producer
    # returns.
    assert sorted(calls) == [("frag", "f1"), ("other", "o1")]


# ---------------------------------------------------------------------------
# N4: Data | None union — both branches via runtime input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_optional_data_return_both_branches():
    """A node typed ``Frag | None`` emits when it returns Data and
    skips when it returns None. Branch selected by input value."""

    @node
    async def consumer(f: Frag) -> None:
        calls.append(f.fid)

    @node
    async def producer(m: M) -> Frag | None:
        if m.text == "emit":
            return Frag(fid=m.mid)
        return None

    wire(M).to(producer)
    wire(Frag).to(consumer)
    compile_graph()

    await emit(M(mid="skip-me", text="no"))
    await emit(M(mid="keep-me", text="emit"))

    assert calls == ["keep-me"]


# ---------------------------------------------------------------------------
# Plain async function without @node: not registered, no auto-emit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_async_function_without_node_does_not_auto_emit():
    """The auto-emit lives in the ``@node`` wrapper. A plain async
    function that returns a Data instance does not emit anything,
    because there is no wrapper around it and it never gets registered
    in ``NODE_REGISTRY``. This guards against the misconception that
    "returning Data" is magic at the language level."""

    @node
    async def consumer(f: Frag) -> None:
        calls.append(f.fid)

    # Note: no @node — this is a plain coroutine function.
    async def naked_producer(m: M) -> Frag:
        return Frag(fid="should-not-leak")

    assert naked_producer not in NODE_REGISTRY

    # Wire a real @node so the graph compiles cleanly; then bypass it
    # and invoke the plain function directly.
    @node
    async def real_producer(m: M) -> None: ...

    wire(M).to(real_producer)
    wire(Frag).to(consumer)
    compile_graph()

    result = await naked_producer(M(mid="m1"))

    assert isinstance(result, Frag)
    assert result.fid == "should-not-leak"
    # The consumer was never reached because no wrapper => no emit.
    assert calls == []
