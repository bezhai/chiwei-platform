"""Tests for wiring/safety.py — verify wires + bind correctness.

B1: ``resolve_pre_safety_waiter`` was removed — PreSafetyVerdict is now
consumed generically by ``emit_and_wait``'s notify() hook, so there is
no longer a dedicated reply-side wire to assert. We keep coverage on:
  * PreSafetyRequest -> run_pre_safety (in-process)
  * PostSafetyRequest -> run_post_safety (in-process transient trigger)
  * Recall -> Sink.mq("recall")
  * agent-service bindings on the two remaining @nodes.
"""
from __future__ import annotations

import importlib


def _fresh_import():
    """Repopulate WIRING_REGISTRY / _BINDINGS from scratch.

    Matches the pattern used by test_memory.py: clear registries, then reload
    to force re-execution of wire/bind statements.
    """
    import app.wiring.safety as s
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(s)


def test_pre_safety_request_wire():
    _fresh_import()

    from app.domain.safety import PreSafetyRequest
    from app.nodes.safety import run_pre_safety
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is PreSafetyRequest]
    assert any(run_pre_safety in w.consumers and w.durable is False for w in wires)


def test_pre_safety_verdict_has_no_dedicated_wire():
    """B1: PreSafetyVerdict consumed by emit_and_wait notify(), not a wire."""
    _fresh_import()

    from app.domain.safety import PreSafetyVerdict
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is PreSafetyVerdict]
    assert wires == [], (
        f"PreSafetyVerdict must have no wires after B1 — generic "
        f"emit_and_wait.notify() handles it. Found: {wires}"
    )


def test_post_safety_request_wire_in_process():
    _fresh_import()

    from app.domain.safety import PostSafetyRequest
    from app.nodes.safety import run_post_safety
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is PostSafetyRequest]
    assert any(run_post_safety in w.consumers and w.durable is False for w in wires)


def test_recall_sink_wire():
    _fresh_import()

    from app.domain.safety import Recall
    from app.runtime.sink import SinkSpec
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is Recall]
    assert any(
        any(
            isinstance(s, SinkSpec) and s.kind == "mq" and s.params.get("queue") == "recall"
            for s in w.sinks
        )
        for w in wires
    )


def test_agent_service_bindings():
    _fresh_import()

    from app.nodes.safety import (
        run_post_safety,
        run_pre_safety,
    )
    from app.runtime.placement import iter_bindings

    b = dict(iter_bindings())
    assert b[run_pre_safety] == "agent-service"
    assert b[run_post_safety] == "agent-service"
