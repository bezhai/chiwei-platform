"""compile_graph 接受 .debounce() canonical shape，并 reject 10 项 anti-pattern。

Spec §3.4.2 / Plan task 4。所有 reject 理由必须有可读的错误消息，
开发者看到错误就知道怎么改 (drop ``.durable()``、改 transient、把 sink 拆出来等)。
"""

from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.graph import GraphError, compile_graph
from app.runtime.node import NODE_REGISTRY, _NODE_META, node
from app.runtime.placement import clear_bindings
from app.runtime.sink import Sink
from app.runtime.source import Source
from app.runtime.wire import WIRING_REGISTRY, WireSpec, clear_wiring, wire


class _T(Data):
    chat_id: Annotated[str, Key]

    class Meta:
        transient = True


class _NotTransient(Data):
    chat_id: Annotated[str, Key]
    # 没有 Meta.transient = True


class _T2(Data):
    chat_id: Annotated[str, Key]

    class Meta:
        transient = True


class _W(Data):
    chat_id: Annotated[str, Key]

    class Meta:
        transient = True


@pytest.fixture(autouse=True)
def _reset():
    """Per-test isolation for the wiring DSL.

    Snapshots NODE_REGISTRY / _NODE_META on entry and restores them on
    exit so module-level ``@node`` decorations registered by other test
    files (which only run their decorator side-effects once at import
    time) survive into later test runs. Wiring + bindings can be cleared
    outright since they're per-test scoped throughout the suite.
    """
    nodes_snap = set(NODE_REGISTRY)
    meta_snap = dict(_NODE_META)
    clear_wiring()
    clear_bindings()
    yield
    clear_wiring()
    clear_bindings()
    NODE_REGISTRY.clear()
    NODE_REGISTRY.update(nodes_snap)
    _NODE_META.clear()
    _NODE_META.update(meta_snap)


def test_debounce_accepts_minimal_legal_form():
    @node
    async def consumer(_t: _T) -> None: ...

    wire(_T).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).to(consumer)
    g = compile_graph()  # 不抛
    assert _T in g.data_types


def test_debounce_rejects_durable():
    @node
    async def consumer(_t: _T) -> None: ...

    wire(_T).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).durable().to(consumer)
    with pytest.raises(GraphError, match="debounce.*durable"):
        compile_graph()


def test_debounce_rejects_as_latest():
    @node
    async def consumer(_t: _T) -> None: ...

    wire(_T).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).as_latest().to(consumer)
    with pytest.raises(GraphError, match="as_latest"):
        compile_graph()


def test_debounce_rejects_with_latest():
    @node
    async def consumer(_t: _T, _w: _W) -> None: ...

    @node
    async def w_producer(_w: _W) -> None: ...

    wire(_W).to(w_producer).as_latest()
    wire(_T).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).with_latest(_W).to(consumer)
    with pytest.raises(GraphError, match="with_latest"):
        compile_graph()


def test_debounce_rejects_when_predicate():
    @node
    async def consumer(_t: _T) -> None: ...

    wire(_T).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).when(lambda x: True).to(consumer)
    with pytest.raises(GraphError, match="DebounceReschedule"):
        compile_graph()


def test_debounce_rejects_sink():
    @node
    async def consumer(_t: _T) -> None: ...

    wire(_T).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).to(Sink.mq("recall"))
    with pytest.raises(GraphError, match="Sink"):
        compile_graph()


def test_debounce_rejects_source():
    @node
    async def consumer(_t: _T) -> None: ...

    wire(_T).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).from_(Source.mq("foo")).to(consumer)
    with pytest.raises(GraphError, match="Source"):
        compile_graph()


def test_debounce_rejects_fanout():
    @node
    async def consumer(_t: _T) -> None: ...

    @node
    async def other(_t: _T) -> None: ...

    wire(_T).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).to(consumer, other)
    with pytest.raises(GraphError, match="exactly one"):
        compile_graph()


def test_debounce_rejects_two_wires_same_datatype():
    @node
    async def consumer(_t: _T) -> None: ...

    @node
    async def consumer2(_t: _T) -> None: ...

    wire(_T).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).to(consumer)
    wire(_T).debounce(
        seconds=120, max_buffer=10, key_by=lambda e: e.chat_id
    ).to(consumer2)
    with pytest.raises(GraphError, match="already declared"):
        compile_graph()


def test_debounce_rejects_non_transient_data():
    @node
    async def consumer(_t: _NotTransient) -> None: ...

    wire(_NotTransient).debounce(
        seconds=60, max_buffer=5, key_by=lambda e: e.chat_id
    ).to(consumer)
    with pytest.raises(GraphError, match="transient"):
        compile_graph()


def test_debounce_rejects_missing_key_by():
    """DSL 层 key_by 必填；测试 graph 层 defensive 校验（构造 spec 时绕过 DSL）。"""

    @node
    async def consumer(_t: _T) -> None: ...

    spec = WireSpec(
        data_type=_T,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 5},
        debounce_key_by=None,  # defensive 路径
    )
    WIRING_REGISTRY.append(spec)
    with pytest.raises(GraphError, match="key_by"):
        compile_graph()
