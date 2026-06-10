"""bootstrap.py: load_dataflow_graph / declare_durable_topology / prepare_for_run.

prepare_for_run is the unified startup helper both entrypoints (FastAPI
lifespan and worker runtime_entry) call so they share the same boot
contract: (1) load + compile the graph, (2) register the runtime-internal
delayed-trigger wire for this app, (3) optionally pre-declare durable
topology for producer-side processes. Without this helper, two entries
each open-coded these phases in slightly different orders and forgot to
keep them in sync (e.g. main.py's lifespan ran load_dataflow_graph BEFORE
register_runtime_trigger_wire, but neither Runtime.run nor runtime_entry
called declare_durable_topology — so the FastAPI process needed extra
boilerplate that worker processes silently skipped).
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
    """load_dataflow_graph() picks up the production wires + bindings,
    not an empty graph.

    Uses clear + reload idiom to get a clean slate before checking that
    real production wires are present.
    """
    import importlib

    import app.deployment as d
    import app.wiring.memory_vectorize as mv
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(mv)
    importlib.reload(d)

    g = load_dataflow_graph()
    # v4 memory vectorize nodes are bound to vectorize-worker
    assert {n.__name__ for n in g.nodes} >= {
        "vectorize_memory_fragment",
        "vectorize_memory_abstract",
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


# ---------------------------------------------------------------------------
# prepare_for_run: unified startup contract for FastAPI + worker entries.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_for_run_loads_graph_then_registers_trigger_wire():
    """prepare_for_run() runs Phase 1 (load_dataflow_graph) BEFORE Phase 1.5
    (register_runtime_trigger_wire). Reverse order would let
    compile_graph snapshot a registry without the trigger wire — exactly
    the boot-order bug both entrypoints used to open-code by hand.
    """
    calls: list[str] = []

    def _fake_load() -> object:
        calls.append("load_dataflow_graph")
        return MagicMock()

    def _fake_register(app: str) -> None:
        calls.append(f"register_runtime_trigger_wire:{app}")

    with patch("app.runtime.bootstrap.load_dataflow_graph", _fake_load), \
         patch(
             "app.runtime.delayed_trigger.register_runtime_trigger_wire",
             _fake_register,
         ):
        await prepare_for_run("agent-service")

    assert calls == [
        "load_dataflow_graph",
        "register_runtime_trigger_wire:agent-service",
    ]


@pytest.mark.asyncio
async def test_prepare_for_run_skips_trigger_wire_for_unknown_app():
    """Apps outside KNOWN_APPS_FOR_DELAYED_TRIGGER must not call
    register_runtime_trigger_wire (which would raise) — they should
    fall through silently, same as Runtime.run does today.
    """
    register_calls: list[str] = []

    def _fake_register(app: str) -> None:
        register_calls.append(app)

    with patch("app.runtime.bootstrap.load_dataflow_graph", MagicMock()), \
         patch(
             "app.runtime.delayed_trigger.register_runtime_trigger_wire",
             _fake_register,
         ):
        await prepare_for_run("some-other-service")

    assert register_calls == []


@pytest.mark.asyncio
async def test_prepare_for_run_declares_topology_when_requested():
    """The FastAPI lifespan is a producer (proactive -> vectorize-worker),
    so it must pre-declare durable routes before publishing. Worker
    entries already declare their own consumer routes via
    start_consumers and don't need this. Flag controls it.
    """
    declare_called: list[bool] = []

    async def _fake_declare() -> None:
        declare_called.append(True)

    with patch("app.runtime.bootstrap.load_dataflow_graph", MagicMock()), \
         patch(
             "app.runtime.delayed_trigger.register_runtime_trigger_wire",
             MagicMock(),
         ), \
         patch(
             "app.runtime.bootstrap.declare_durable_topology",
             _fake_declare,
         ):
        await prepare_for_run("agent-service", declare_topology=True)

    assert declare_called == [True]


@pytest.mark.asyncio
async def test_prepare_for_run_skips_topology_by_default():
    """Default declare_topology=False — worker entries should not force a
    broker connection at this phase.
    """
    declare_called: list[bool] = []

    async def _fake_declare() -> None:
        declare_called.append(True)

    with patch("app.runtime.bootstrap.load_dataflow_graph", MagicMock()), \
         patch(
             "app.runtime.delayed_trigger.register_runtime_trigger_wire",
             MagicMock(),
         ), \
         patch(
             "app.runtime.bootstrap.declare_durable_topology",
             _fake_declare,
         ):
        await prepare_for_run("agent-service")

    assert declare_called == []
