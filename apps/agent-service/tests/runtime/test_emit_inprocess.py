from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.graph import compile_graph
from app.runtime.node import node
from app.runtime.wire import clear_wiring, wire


class M(Data):
    mid: Annotated[str, Key]
    text: str


calls: list = []


def setup_function():
    clear_wiring()
    calls.clear()
    reset_emit_runtime()


@pytest.mark.asyncio
async def test_emit_default_edge_awaits_consumer():
    @node
    async def recorder(m: M) -> None:
        calls.append(m)

    wire(M).to(recorder)  # default (in-process)
    compile_graph()
    await emit(M(mid="m1", text="hi"))
    assert len(calls) == 1
    assert calls[0].text == "hi"


@pytest.mark.asyncio
async def test_emit_when_predicate_filters():
    @node
    async def only_x(m: M) -> None:
        calls.append(m)

    wire(M).to(only_x).when(lambda m: m.mid == "x")
    compile_graph()
    await emit(M(mid="y", text="skip"))
    await emit(M(mid="x", text="keep"))
    assert [c.text for c in calls] == ["keep"]


@pytest.mark.asyncio
async def test_emit_multiple_consumers_fan_out():
    @node
    async def a(m: M) -> None:
        calls.append(("a", m.text))

    @node
    async def b(m: M) -> None:
        calls.append(("b", m.text))

    wire(M).to(a)
    wire(M).to(b)
    compile_graph()
    await emit(M(mid="m1", text="x"))
    assert sorted(calls) == [("a", "x"), ("b", "x")]


@pytest.mark.asyncio
async def test_emit_no_matching_wire_is_noop():
    # No wire for M; emit should silently no-op (no exception).
    compile_graph()
    await emit(M(mid="m1", text="x"))
    assert calls == []


@pytest.mark.asyncio
async def test_emit_skips_in_process_consumer_bound_to_other_app(monkeypatch):
    """In-process consumers bound to a worker app must NOT run in the
    main process. bind(...).to_app() is the placement contract — emit
    has to honour it the same way the durable consumer / source loop
    already do, otherwise a main-process emit would silently run a
    worker-only @node here.
    """
    from app.runtime.placement import bind

    @node
    async def worker_only(m: M) -> None:
        calls.append(m)

    bind(worker_only).to_app("vectorize-worker")
    wire(M).to(worker_only)  # in-process
    compile_graph()

    # Main process: APP_NAME unset / DEFAULT_APP. worker_only bound to
    # vectorize-worker -> emit must skip it.
    monkeypatch.delenv("APP_NAME", raising=False)
    await emit(M(mid="m1", text="hi"))
    assert calls == []

    # Same emit from inside the bound worker process: should run.
    monkeypatch.setenv("APP_NAME", "vectorize-worker")
    await emit(M(mid="m1", text="hi"))
    assert len(calls) == 1
    assert calls[0].text == "hi"
