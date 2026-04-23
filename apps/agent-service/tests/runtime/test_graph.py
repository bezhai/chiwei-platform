from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import AdminOnly, Data, Key
from app.runtime.graph import GraphError, compile_graph
from app.runtime.node import node
from app.runtime.wire import clear_wiring, wire


class M(Data):
    mid: Annotated[str, Key]


class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict


class S(Data):
    sid: Annotated[str, Key]
    v: int


def setup_function():
    clear_wiring()


def test_compile_success():
    @node
    async def f(m: M) -> None: ...

    wire(M).to(f)
    g = compile_graph()
    assert M in g.data_types
    assert f in g.nodes


def test_consumer_signature_mismatch_rejected():
    @node
    async def takes_m(m: M) -> None: ...

    # Wire declares M -> takes_m, consumer accepts M. Should pass.
    wire(M).to(takes_m)
    compile_graph()  # no error


def test_admin_only_consumer_ok():
    # AdminOnly can be consumed (read-only), just not produced.
    @node
    async def reads_cfg(c: Cfg) -> None: ...

    wire(Cfg).to(reads_cfg)
    compile_graph()  # ok


def test_wire_to_unknown_node_rejected():
    async def not_a_node(m: M) -> None: ...

    wire(M).to(not_a_node)
    with pytest.raises(GraphError, match="not registered"):
        compile_graph()


def test_with_latest_requires_as_latest_declared():
    # S is defined at module top so @node's get_type_hints can resolve the annotation.
    @node
    async def f(m: M, s: S) -> None: ...

    wire(M).to(f).with_latest(S)
    # S has no wire(S).as_latest() declaration anywhere
    with pytest.raises(GraphError, match="with_latest.*requires.*as_latest"):
        compile_graph()


def test_consumer_missing_data_type_param_rejected():
    # Consumer only accepts Cfg, but wire routes M to it -> signature mismatch.
    @node
    async def wrong(c: Cfg) -> None: ...

    wire(M).to(wrong)
    with pytest.raises(GraphError, match="does not accept"):
        compile_graph()


def test_consumer_missing_with_latest_param_rejected():
    # Consumer accepts M but not S; wire asks for with_latest(S) -> signature mismatch.
    @node
    async def takes_only_m(m: M) -> None: ...

    @node
    async def s_producer(s: S) -> None: ...

    wire(S).to(s_producer).as_latest()
    wire(M).to(takes_only_m).with_latest(S)
    with pytest.raises(GraphError, match="does not accept"):
        compile_graph()
