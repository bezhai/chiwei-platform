"""Cross-module invariant: header lane == queue lane, every publish.

``mq.publish`` picks the queue with ``current_lane()`` — contextvar first,
``LANE`` env second. ``inject_context`` (no explicit Context) reads the
contextvar only. On a lane pod there is a window where those two disagree:
a background task / startup-time emit has no contextvar, so the message
lands in ``xxx_ppe-x`` while its header says ``lane: ""``.

That combination is exactly the bug this module guards. The consuming side
is header-only by design (a prod pod also drains messages that TTL'd back
from a lane queue, so it cannot fall back to its own ``LANE``), so a
lane-queue message carrying an empty lane header gets processed as prod and
every outbound call in the consuming process goes to prod.

The tests assert the invariant directly rather than the implementation:
whatever lane ends up in the header, the routing key + lazily-declared
queue that the broker actually saw must be the ones derived from THAT lane.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated

import pytest

from app.infra.rabbitmq import Route
from app.runtime import Data, Key, Sink, Source, bind, emit, node, wire
from app.runtime.placement import clear_bindings
from app.runtime.propagation import Context, bind_context
from app.runtime.wire import WireSpec, clear_wiring

LANE = "ppe-lanecheck"


# ---------------------------------------------------------------------------
# Probe Data classes (module level so @node's get_type_hints() resolves them)
# ---------------------------------------------------------------------------


class _SourceMqProbe(Data):
    """Rides ``runtime_delayed_trigger_agent-service``, a queue that really exists in ALL_ROUTES."""

    chat_id: Annotated[str, Key]

    class Meta:
        transient = True


class _SinkProbe(Data):
    session_id: Annotated[str, Key]
    # recall 按 channel 分区，dispatch 据它选 rk。
    channel: str = "lark"

    class Meta:
        transient = True


class _DebounceProbe(Data):
    chat_id: Annotated[str, Key]

    class Meta:
        transient = True


class _ReviewProbe(Data):
    id: Annotated[str, Key]

    class Meta:
        transient = True


async def _debounce_consumer(t: _DebounceProbe) -> None:
    return None


def _review_consumer() -> None:
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate():
    clear_wiring()
    clear_bindings()
    from app.runtime.emit import reset_emit_runtime

    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


@pytest.fixture
def broker(monkeypatch):
    """Run the real ``mq.publish`` against a stubbed exchange / channel.

    Patching ``mq.publish`` itself would hide the very thing under test —
    the queue name is computed *inside* publish. So we stub one layer
    lower and read the routing key + headers the broker would have seen.
    """
    from app.infra.rabbitmq import mq

    declared: list[str] = []
    published: list[tuple[str, object]] = []

    class _FakeQueue:
        async def bind(self, exchange, routing_key=None):
            return None

    class _FakeChannel:
        async def declare_queue(self, name, durable=True, arguments=None):
            declared.append(name)
            return _FakeQueue()

    class _FakeExchange:
        async def publish(self, message, routing_key=None):
            published.append((routing_key, message))

    monkeypatch.setattr(mq, "_exchange", _FakeExchange())
    monkeypatch.setattr(mq, "_channel", _FakeChannel())
    monkeypatch.setattr(mq, "_declared_lane_queues", set())
    return SimpleNamespace(declared=declared, published=published)


@pytest.fixture
def lane_env(monkeypatch):
    """The broken combination: no contextvar lane, ``LANE`` env set."""
    monkeypatch.setenv("LANE", LANE)
    monkeypatch.setenv("APP_NAME", "agent-service")


def assert_header_lane_drives_the_queue(broker, route: Route) -> str | None:
    """Assert the routing key / queue the broker saw match the header lane.

    Returns the header lane so callers can additionally pin its value.
    """
    assert len(broker.published) == 1, (
        f"expected exactly one publish, got {len(broker.published)}"
    )
    rk, message = broker.published[0]
    headers = dict(message.headers or {})
    header_lane = headers.get("lane") or None

    expected_rk = f"{route.rk}.{header_lane}" if header_lane else route.rk
    assert rk == expected_rk, (
        f"header says lane={header_lane!r} but the message was routed with "
        f"{rk!r} (expected {expected_rk!r}) — the queue lane and the header "
        f"lane come from different sources, so the consumer will treat this "
        f"message as belonging to the wrong lane"
    )
    if header_lane:
        assert f"{route.queue}_{header_lane}" in broker.declared, (
            f"lane queue for {header_lane!r} was never declared; declared="
            f"{broker.declared}"
        )
    return header_lane


# ---------------------------------------------------------------------------
# 1. emit() -> Source.mq cross-process publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_source_mq_header_lane_matches_queue_lane(broker, lane_env):
    from app.infra.rabbitmq import trigger_route_for

    @node
    async def _remote_handler(r: _SourceMqProbe) -> None:
        pass

    wire(_SourceMqProbe).to(_remote_handler).from_(Source.mq("runtime_delayed_trigger_agent-service"))
    bind(_remote_handler).to_app("some-other-app")

    async with bind_context(Context(trace_id=None, lane=None)):
        await emit(_SourceMqProbe(chat_id="c1"))

    assert (
        assert_header_lane_drives_the_queue(
            broker, trigger_route_for("agent-service")
        )
        == LANE
    )


# ---------------------------------------------------------------------------
# 2. emit() -> Sink.mq dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_dispatch_header_lane_matches_queue_lane(broker, lane_env):
    from app.infra.rabbitmq import RECALL, channel_route

    wire(_SinkProbe).to(Sink.mq("recall"))

    async with bind_context(Context(trace_id=None, lane=None)):
        await emit(_SinkProbe(session_id="s1"))

    # 加了 channel 维度之后泳道后缀仍然加在最后：recall_lark_{lane} / action.recall.lark.{lane}
    expected = channel_route(RECALL, "lark")
    assert assert_header_lane_drives_the_queue(broker, expected) == LANE


# ---------------------------------------------------------------------------
# 3 + 4. debounce publish + reschedule
# ---------------------------------------------------------------------------


def _debounce_wire() -> WireSpec:
    return WireSpec(
        data_type=_DebounceProbe,
        consumers=[_debounce_consumer],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"probe:{e.chat_id}",
    )


@pytest.fixture
def fake_redis(monkeypatch):
    from unittest.mock import AsyncMock

    redis = AsyncMock()
    # publish path returns [count, fire_now]; CAS swap path returns 1
    redis.eval = AsyncMock(return_value=[1, 0])
    monkeypatch.setattr(
        "app.runtime.debounce.get_redis", AsyncMock(return_value=redis)
    )
    return redis


@pytest.mark.asyncio
async def test_debounce_publish_header_lane_matches_queue_lane(
    broker, lane_env, fake_redis
):
    from app.runtime.debounce import _route_for, publish_debounce

    w = _debounce_wire()
    await publish_debounce(w, _debounce_consumer, _DebounceProbe(chat_id="c1"))

    route = _route_for(w, _debounce_consumer)
    assert assert_header_lane_drives_the_queue(broker, route) == LANE


@pytest.mark.asyncio
async def test_debounce_reschedule_header_lane_matches_queue_lane(
    broker, lane_env, fake_redis
):
    from app.runtime.debounce import _do_reschedule, _route_for

    fake_redis.eval = type(fake_redis.eval)(return_value=1)  # CAS swap ok

    w = _debounce_wire()
    await _do_reschedule(
        w, _debounce_consumer, _DebounceProbe(chat_id="c1"), "orig-trigger"
    )

    route = _route_for(w, _debounce_consumer)
    assert assert_header_lane_drives_the_queue(broker, route) == LANE


# ---------------------------------------------------------------------------
# 5. manual-review queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_queue_header_lane_matches_queue_lane(broker, lane_env):
    from app.runtime.review_queue import (
        publish_to_review_queue,
        review_queue_name_for,
        route_for_review,
    )

    spec = WireSpec(
        data_type=_ReviewProbe,
        consumers=[_review_consumer],
        durable=True,
        on_error="manual-review",
    )
    confirmed = await publish_to_review_queue(
        wire=spec,
        consumer=_review_consumer,
        data=_ReviewProbe(id="x"),
        exc=RuntimeError("boom"),
        attempts=2,
        last_error="boom",
    )
    assert confirmed is True

    route = route_for_review(review_queue_name_for(spec, _review_consumer))
    assert assert_header_lane_drives_the_queue(broker, route) == LANE
