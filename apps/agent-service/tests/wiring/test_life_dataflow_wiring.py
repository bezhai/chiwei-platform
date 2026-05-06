"""Phase 4 life_dataflow wiring smoke test."""
from __future__ import annotations

import importlib


def _fresh_import():
    """Repopulate WIRING_REGISTRY from scratch by reloading life_dataflow.

    Matches the pattern used by test_safety_wiring.py: clear registries, then reload
    to force re-execution of wire statements.
    """
    import app.wiring.life_dataflow as ld
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(ld)


def test_life_dataflow_wiring_compiles():
    """Loading the wiring module must produce a graph that compiles."""
    _fresh_import()

    from app.runtime.graph import compile_graph

    graph = compile_graph()  # raises GraphError on misconfig
    assert graph is not None


def test_life_dataflow_wire_count_is_15():
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    # 5 cron Tick + GlimpseTick + SharedDailyContext + DailyPlanRequest +
    # 4 PersonaXxxRequest + GlimpseTickRequest + LifeStateChanged + GlimpseRequest
    # = 6 + 1 + 1 + 4 + 1 + 1 + 1 = 15
    types = {w.data_type.__name__ for w in WIRING_REGISTRY}
    expected = {
        "MinuteTick", "LightDayTick", "LightNightTick", "HeavyReviewTick",
        "DailyPlanTick", "GlimpseTick",
        "SharedDailyContext", "DailyPlanRequest",
        "LifeTickRequest", "VoiceRequest", "LightReviewRequest",
        "HeavyReviewRequest", "GlimpseTickRequest",
        "LifeStateChanged", "GlimpseRequest",
    }
    assert types == expected
    assert len(WIRING_REGISTRY) == 15


def test_glimpse_request_wire_is_durable():
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    glimpse_req_wires = [w for w in WIRING_REGISTRY if w.data_type.__name__ == "GlimpseRequest"]
    assert len(glimpse_req_wires) == 1
    assert glimpse_req_wires[0].durable is True
