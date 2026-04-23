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


@node
async def recorder(m: M) -> None:
    calls.append(m)


def setup_function():
    clear_wiring()
    calls.clear()
    reset_emit_runtime()


@pytest.mark.asyncio
async def test_emit_default_edge_awaits_consumer():
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
