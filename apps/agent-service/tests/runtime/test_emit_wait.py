"""``emit_and_wait`` — process-local request/reply on the dataflow graph (B1).

Tests the runtime primitive that lets a caller emit a request Data, then
await a typed reply Data correlated by a named field. Replaces the global
``_waiters`` dict + hand-rolled future in ``app/chat/pre_safety_gate.py``.

Surface contract:
  * emit_and_wait(req, *, wait_for, correlation, correlation_field, timeout_s)
  * Resolves when emit() dispatches a Data of type ``wait_for`` whose
    ``correlation_field`` equals ``correlation``.
  * Raises ``EmitWaitTimeout`` on deadline; cleans up registry.
  * Cooperative cancel propagates and cleans up registry.
  * Concurrent waiters with different correlations do not cross-fire.
  * Mid-flight emit failure inside the dispatch chain still surfaces as
    the original exception (does not silently hang the waiter).
"""
from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.emit_wait import EmitWaitTimeout, emit_and_wait
from app.runtime.graph import compile_graph
from app.runtime.node import node
from app.runtime.wire import clear_wiring, wire


class Req(Data):
    cid: Annotated[str, Key]
    payload: str

    class Meta:
        transient = True


class Reply(Data):
    cid: Annotated[str, Key]
    result: str

    class Meta:
        transient = True


def setup_function():
    clear_wiring()
    reset_emit_runtime()


@pytest.mark.asyncio
async def test_emit_and_wait_resolves_when_reply_dispatched():
    """req -> worker emits Reply matching cid -> emit_and_wait returns it."""

    @node
    async def worker(r: Req) -> Reply:
        # @node auto-emits the returned Data (B8).
        return Reply(cid=r.cid, result=f"echo:{r.payload}")

    wire(Req).to(worker)
    compile_graph()

    got = await emit_and_wait(
        Req(cid="c1", payload="hi"),
        wait_for=Reply,
        correlation="c1",
        correlation_field="cid",
        timeout_s=2.0,
    )
    assert isinstance(got, Reply)
    assert got.result == "echo:hi"


@pytest.mark.asyncio
async def test_emit_and_wait_times_out_when_no_reply():
    """No matching reply ever arrives -> EmitWaitTimeout, registry cleaned."""

    @node
    async def silent(r: Req) -> None:
        return None  # never emits Reply

    wire(Req).to(silent)
    compile_graph()

    with pytest.raises(EmitWaitTimeout):
        await emit_and_wait(
            Req(cid="cTimeout", payload="x"),
            wait_for=Reply,
            correlation="cTimeout",
            correlation_field="cid",
            timeout_s=0.1,
        )

    # After timeout the waiter must be gone — emitting a late Reply with
    # the same correlation must NOT crash (no dangling future to set).
    from app.runtime import emit_wait as _ew

    assert not any(
        key[1] == "cTimeout" for key in _ew._waiters
    ), "timed-out waiter must be cleaned up"
    await emit(Reply(cid="cTimeout", result="late"))  # must not raise


@pytest.mark.asyncio
async def test_emit_and_wait_concurrent_correlations_do_not_cross():
    """Two concurrent waiters with different correlations both resolve to
    their own reply — wrong-correlation replies are ignored."""

    @node
    async def worker(r: Req) -> Reply:
        return Reply(cid=r.cid, result=r.payload.upper())

    wire(Req).to(worker)
    compile_graph()

    a = asyncio.create_task(
        emit_and_wait(
            Req(cid="A", payload="a"),
            wait_for=Reply,
            correlation="A",
            correlation_field="cid",
            timeout_s=2.0,
        )
    )
    b = asyncio.create_task(
        emit_and_wait(
            Req(cid="B", payload="b"),
            wait_for=Reply,
            correlation="B",
            correlation_field="cid",
            timeout_s=2.0,
        )
    )
    ra, rb = await asyncio.gather(a, b)
    assert ra.result == "A"
    assert rb.result == "B"


@pytest.mark.asyncio
async def test_emit_and_wait_wrong_correlation_does_not_resolve():
    """A Reply with non-matching cid must NOT resolve the waiter."""

    @node
    async def wrong_reply(r: Req) -> Reply:
        return Reply(cid="WRONG_CID", result="oops")

    wire(Req).to(wrong_reply)
    compile_graph()

    with pytest.raises(EmitWaitTimeout):
        await emit_and_wait(
            Req(cid="want", payload="x"),
            wait_for=Reply,
            correlation="want",
            correlation_field="cid",
            timeout_s=0.2,
        )


@pytest.mark.asyncio
async def test_emit_and_wait_caller_cancellation_cleans_registry():
    """Outer-task cancel must propagate AND clean the registry."""

    @node
    async def slow(r: Req) -> None:
        # Never reply — keep the waiter pending.
        await asyncio.sleep(10)

    wire(Req).to(slow)
    compile_graph()

    task = asyncio.create_task(
        emit_and_wait(
            Req(cid="cancelme", payload="x"),
            wait_for=Reply,
            correlation="cancelme",
            correlation_field="cid",
            timeout_s=10.0,
        )
    )
    # Yield once so the inner emit/register runs.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    from app.runtime import emit_wait as _ew

    assert not any(
        key[1] == "cancelme" for key in _ew._waiters
    ), "cancelled waiter must be cleaned up"


@pytest.mark.asyncio
async def test_emit_and_wait_emit_chain_failure_surfaces():
    """If the dispatch chain raises before any reply is emitted, the
    waiter must not silently hang to timeout — it should surface the
    original error."""

    @node
    async def boom(r: Req) -> None:
        raise RuntimeError("downstream blew up")

    wire(Req).to(boom)
    compile_graph()

    with pytest.raises(RuntimeError, match="downstream blew up"):
        await emit_and_wait(
            Req(cid="boom", payload="x"),
            wait_for=Reply,
            correlation="boom",
            correlation_field="cid",
            timeout_s=2.0,
        )

    from app.runtime import emit_wait as _ew

    assert not any(
        key[1] == "boom" for key in _ew._waiters
    ), "registry must be cleaned up after emit-chain failure"


@pytest.mark.asyncio
async def test_emit_and_wait_late_reply_after_done_is_safe():
    """Once the waiter is resolved + popped, a second matching Reply must
    not crash (no dangling future / KeyError)."""

    @node
    async def worker(r: Req) -> Reply:
        return Reply(cid=r.cid, result="ok")

    wire(Req).to(worker)
    compile_graph()

    got = await emit_and_wait(
        Req(cid="once", payload="x"),
        wait_for=Reply,
        correlation="once",
        correlation_field="cid",
        timeout_s=2.0,
    )
    assert got.result == "ok"

    # Second emit with the same correlation — registry is empty, must no-op.
    await emit(Reply(cid="once", result="late"))
