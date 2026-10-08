"""bootstrap.py: load_dataflow_graph / declare_durable_topology / prepare_for_run.

prepare_for_run is the startup helper ``app.main``'s lifespan calls:
(1) load + compile the graph, (2) optionally pre-declare durable topology
for a process that publishes.
"""

from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.runtime.bootstrap import (
    declare_durable_topology,
    load_dataflow_graph,
    prepare_for_run,
)
from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.wire import wire


class _Probe(Data):
    pid: Annotated[str, Key]


def test_load_dataflow_graph_returns_compiled_graph_with_real_wiring():
    """load_dataflow_graph("agent-service") picks up the production wires +
    bindings, not an empty graph.

    Uses clear + reload idiom to get a clean slate before checking that
    real production wires are present.
    """
    import importlib

    import app.deployment as d
    import app.wiring.living as lw
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(lw)
    importlib.reload(d)

    g = load_dataflow_graph("agent-service")
    # living 的钟骑在生产 wiring 上
    assert {n.__name__ for n in g.nodes} >= {
        "life_moment_tick",
        "phone_nudge_tick",
        "day_page_tick",
    }


@pytest.mark.asyncio
async def test_declare_durable_topology_declares_no_route_without_durable_wire():
    """No durable wire means no route to declare -- but the exchange still
    has to be there.

    Callers only reach this function through
    ``prepare_for_run(declare_topology=True)``, and that flag already means
    "this process is going to publish". Skipping the exchange because no
    durable *consumer* happens to be registered conflates two unrelated
    things and leaves ``mq.publish`` raising "must call declare_topology()
    first" -- see the companion test below for how that bites.
    """
    with patch("app.infra.rabbitmq.mq") as mock_mq:
        mock_mq.connect = AsyncMock()
        mock_mq.declare_topology = AsyncMock()
        mock_mq.declare_route = AsyncMock()
        await declare_durable_topology()

    mock_mq.declare_route.assert_not_called()


@pytest.mark.asyncio
async def test_a_process_with_only_outbound_mq_can_still_publish():
    """A process whose every ``Source.mq`` is gone still needs its exchange.

    This is not hypothetical. The living engine registers interval sources
    and zero MQ consumers, so a process can end up holding no durable wire
    at all. It still emits ``ChatResponseSegment`` to the channel worker,
    and without the exchange every one of those publishes died with
    ``RuntimeError: must call declare_topology() first``: she could hear
    but not speak.
    """
    with patch("app.infra.rabbitmq.mq") as mock_mq:
        mock_mq.connect = AsyncMock()
        mock_mq.declare_topology = AsyncMock()
        mock_mq.declare_route = AsyncMock()
        await declare_durable_topology()

    mock_mq.connect.assert_awaited(), "没连 broker，出站无从谈起"
    mock_mq.declare_topology.assert_awaited(), (
        "exchange 没准备好 —— publish 会抛 must call declare_topology() first"
    )


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


# ---------------------------------------------------------------------------
# prepare_for_run: the lifespan's startup helper.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_for_run_declares_topology_when_requested():
    """A process that publishes must pre-declare durable routes before
    publishing; the lifespan asks for it when a broker is configured.
    Flag controls it.
    """
    declare_called: list[bool] = []

    async def _fake_declare() -> None:
        declare_called.append(True)

    with patch("app.runtime.bootstrap.load_dataflow_graph", MagicMock()), \
         patch(
             "app.runtime.bootstrap.declare_durable_topology",
             _fake_declare,
         ):
        await prepare_for_run("agent-service", declare_topology=True)

    assert declare_called == [True]


@pytest.mark.asyncio
async def test_prepare_for_run_skips_topology_by_default():
    """Default declare_topology=False — a process without a broker must
    not be forced to connect at this phase.
    """
    declare_called: list[bool] = []

    async def _fake_declare() -> None:
        declare_called.append(True)

    with patch("app.runtime.bootstrap.load_dataflow_graph", MagicMock()), \
         patch(
             "app.runtime.bootstrap.declare_durable_topology",
             _fake_declare,
         ):
        await prepare_for_run("agent-service")

    assert declare_called == []
