"""bootstrap.py: load_dataflow_graph / declare_durable_topology contracts."""

from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.bootstrap import declare_durable_topology, load_dataflow_graph
from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.wire import wire


class _Probe(Data):
    pid: Annotated[str, Key]


def test_load_dataflow_graph_returns_compiled_graph_with_real_wiring():
    """load_dataflow_graph() picks up the production wires + bindings,
    not an empty graph.

    Same import / clear / reload idiom as ``tests/wiring/test_memory``:
    first import triggers each module body once (which would otherwise
    leave the registries pre-populated and fight the fixture's clear);
    the second clear + reload then repopulates from a clean slate.
    """
    import importlib

    import app.deployment as d
    import app.wiring.memory as m
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(m)
    importlib.reload(d)

    g = load_dataflow_graph()
    assert any(w.data_type.__name__ == "Message" for w in g.wires)
    # hydrate_message + vectorize + save_fragment are all bound
    assert {n.__name__ for n in g.nodes} >= {
        "hydrate_message",
        "vectorize",
        "save_fragment",
    }


@pytest.mark.asyncio
async def test_declare_durable_topology_skips_when_no_durable_wire():
    """Empty registry must not force a broker connection."""
    with patch("app.infra.rabbitmq.mq") as mock_mq:
        mock_mq.connect = AsyncMock()
        mock_mq.declare_topology = AsyncMock()
        mock_mq.declare_route = AsyncMock()
        await declare_durable_topology()

    mock_mq.connect.assert_not_called()
    mock_mq.declare_topology.assert_not_called()
    mock_mq.declare_route.assert_not_called()


@pytest.mark.asyncio
async def test_declare_durable_topology_declares_each_route():
    """Every (data, consumer) pair on a .durable() wire gets one
    declare_route call — so a producer that boots before any consumer
    pod still publishes onto a real route.
    """
    @node
    async def consumer_a(p: _Probe) -> None: ...

    @node
    async def consumer_b(p: _Probe) -> None: ...

    wire(_Probe).to(consumer_a, consumer_b).durable()

    with patch("app.infra.rabbitmq.mq") as mock_mq:
        mock_mq.connect = AsyncMock()
        mock_mq.declare_topology = AsyncMock()
        mock_mq.declare_route = AsyncMock()
        await declare_durable_topology()

    mock_mq.connect.assert_awaited_once()
    mock_mq.declare_topology.assert_awaited_once()
    assert mock_mq.declare_route.await_count == 2
    routes = [call.args[0] for call in mock_mq.declare_route.await_args_list]
    queues = sorted(r.queue for r in routes)
    # Class name "_Probe" snake-cases to "_probe", so the route prefix
    # is durable_ + _probe + _<consumer>; that's three underscores in a row.
    assert queues == ["durable___probe_consumer_a", "durable___probe_consumer_b"]
