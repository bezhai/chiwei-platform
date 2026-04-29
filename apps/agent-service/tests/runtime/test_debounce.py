import pytest
from unittest.mock import AsyncMock, MagicMock

from app.runtime.debounce import (
    DebounceReschedule, _route_for, _DEFAULT_TTL_SECONDS, publish_debounce,
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


def _make_wire():
    return WireSpec(
        data_type=DriftTrigger,
        consumers=[_drift_check_stub],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"drift:{e.chat_id}:{e.persona_id}",
    )


@pytest.mark.asyncio
async def test_publish_debounce_single_event(monkeypatch):
    """单事件：写 latest + INCR count=1 + publish delay=60s 消息。"""
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=[1, 0])
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.debounce.mq",
                        MagicMock(publish=fake_publish))
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: "tr-1"))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    w = _make_wire()
    await publish_debounce(w, _drift_check_stub, DriftTrigger(chat_id="c1", persona_id="p1"))

    fake_redis.eval.assert_awaited_once()
    args = fake_redis.eval.call_args
    assert args.args[1] == 2  # numkeys
    assert "debounce:latest:DriftTrigger:drift:c1:p1" in args.args
    assert "debounce:count:DriftTrigger:drift:c1:p1" in args.args
    assert 86400 in args.args
    assert 3 in args.args

    fake_publish.assert_awaited_once()
    pub_args = fake_publish.call_args
    body = pub_args.args[1]
    assert body["fire_now"] is False
    assert body["data"] == {"chat_id": "c1", "persona_id": "p1"}
    assert body["key"] == "drift:c1:p1"
    assert pub_args.kwargs["delay_ms"] == 60_000


@pytest.mark.asyncio
async def test_publish_debounce_max_buffer_triggers_fire_now(monkeypatch):
    """count 达 max_buffer 时 publish_debounce 拿到 fire_now=1，
    publish delay=0 + body.fire_now=True。"""
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=[3, 1])
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.debounce.mq",
                        MagicMock(publish=fake_publish))
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: ""))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    w = _make_wire()
    await publish_debounce(w, _drift_check_stub, DriftTrigger(chat_id="c1", persona_id="p1"))

    pub_args = fake_publish.call_args
    body = pub_args.args[1]
    assert body["fire_now"] is True
    assert pub_args.kwargs["delay_ms"] == 0
