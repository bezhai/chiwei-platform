from app.runtime.debounce import (
    DebounceReschedule, _route_for, _DEFAULT_TTL_SECONDS,
)
from app.runtime.wire import WireSpec
from app.domain.memory_triggers import DriftTrigger


async def _drift_check_stub(t: DriftTrigger) -> None:
    return None


def test_default_ttl_is_24h():
    assert _DEFAULT_TTL_SECONDS == 86400


def test_route_for_uses_lane_fallback_false():
    spec = WireSpec(
        data_type=DriftTrigger,
        consumers=[_drift_check_stub],
        debounce={"seconds": 60, "max_buffer": 5},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )
    route = _route_for(spec, _drift_check_stub)
    assert route.queue == "debounce_drift_trigger__drift_check_stub"
    assert route.rk == "debounce.drift_trigger.__drift_check_stub" or \
           route.rk == "debounce.drift_trigger._drift_check_stub"
    assert route.lane_fallback is False


def test_debounce_reschedule_carries_data():
    t = DriftTrigger(chat_id="c1", persona_id="p1")
    exc = DebounceReschedule(t)
    assert exc.data == t
    assert "DriftTrigger" in str(exc)


def test_no_module_level_reschedule_function():
    """API 边界：业务节点不应拿到 module-level reschedule()，
    避免 contextvar 泄漏到 background task (reviewer round-7 M1)."""
    import app.runtime.debounce as mod
    assert not hasattr(mod, "reschedule")
