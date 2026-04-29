"""Tests for app.infra.rabbitmq pure functions and constants."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infra.rabbitmq import (
    ALL_ROUTES,
    CHAT_REQUEST,
    CHAT_RESPONSE,
    DLX_NAME,
    EXCHANGE_NAME,
    MEMORY_ABSTRACT_VECTORIZE,
    MEMORY_FRAGMENT_VECTORIZE,
    RECALL,
    VECTORIZE,
    Route,
    _LANE_FALLBACK_TTL_MS,
    _NON_PROD_EXPIRES_MS,
    _build_queue_args,
    _lane_rk,
    current_lane,
    lane_queue,
)


# ---------------------------------------------------------------------------
# _lane_queue / _lane_rk
# ---------------------------------------------------------------------------
class TestLaneQueue:
    """lane_queue appends lane suffix when lane is non-None."""

    def test_prod_returns_base(self):
        assert lane_queue("chat_request", None) == "chat_request"

    def test_lane_appends_suffix(self):
        assert lane_queue("chat_request", "dev") == "chat_request_dev"

    def test_lane_with_hyphen(self):
        assert lane_queue("recall", "feat-v2") == "recall_feat-v2"


class TestLaneRk:
    """_lane_rk appends lane as a dotted segment."""

    def test_prod_returns_base(self):
        assert _lane_rk("chat.request", None) == "chat.request"

    def test_lane_appends_dot_segment(self):
        assert _lane_rk("chat.request", "dev") == "chat.request.dev"

    def test_lane_with_hyphen(self):
        assert _lane_rk("action.recall", "feat-v2") == "action.recall.feat-v2"


# ---------------------------------------------------------------------------
# _build_queue_args
# ---------------------------------------------------------------------------
class TestBuildQueueArgs:
    """_build_queue_args returns correct DLX/TTL/expire args."""

    def test_prod_queue_has_dlx(self):
        args = _build_queue_args("chat.request", None)
        assert args["x-dead-letter-exchange"] == DLX_NAME

    def test_prod_queue_no_ttl(self):
        args = _build_queue_args("chat.request", None)
        assert "x-message-ttl" not in args

    def test_prod_queue_no_expires(self):
        args = _build_queue_args("chat.request", None)
        assert "x-expires" not in args

    def test_lane_queue_has_ttl(self):
        args = _build_queue_args("chat.request", "dev")
        assert args["x-message-ttl"] == 10_000

    def test_lane_queue_fallback_to_main_exchange(self):
        args = _build_queue_args("chat.request", "dev")
        assert args["x-dead-letter-exchange"] == EXCHANGE_NAME

    def test_lane_queue_fallback_rk_is_prod(self):
        """Dead-lettered messages route back to the prod routing key."""
        args = _build_queue_args("chat.request", "dev")
        assert args["x-dead-letter-routing-key"] == "chat.request"

    def test_lane_queue_auto_expires(self):
        args = _build_queue_args("chat.request", "dev")
        assert args["x-expires"] == 86_400_000

    def test_lane_queue_no_dlx_to_dlq(self):
        """Lane queues should NOT dead-letter to DLX (they fallback to prod)."""
        args = _build_queue_args("chat.request", "dev")
        assert args["x-dead-letter-exchange"] != DLX_NAME


# ---------------------------------------------------------------------------
# current_lane
# ---------------------------------------------------------------------------
class TestCurrentLane:
    """current_lane reads from HTTP context or LANE env var."""

    def test_no_env_returns_none(self):
        with patch.dict("os.environ", {}, clear=False):
            os_env = {"LANE": ""}
            with patch.dict("os.environ", os_env):
                with patch(
                    "app.infra.rabbitmq.current_lane",
                    wraps=current_lane,
                ):
                    # Mock get_lane to return None (no trace context)
                    with patch(
                        "app.api.middleware.get_lane",
                        return_value=None,
                    ):
                        result = current_lane()
                        assert result is None

    def test_env_lane_dev(self):
        with patch(
            "app.api.middleware.get_lane",
            return_value=None,
        ):
            with patch.dict("os.environ", {"LANE": "dev"}):
                result = current_lane()
                assert result == "dev"

    def test_env_lane_prod_returns_none(self):
        """'prod' is equivalent to no lane."""
        with patch(
            "app.api.middleware.get_lane",
            return_value=None,
        ):
            with patch.dict("os.environ", {"LANE": "prod"}):
                result = current_lane()
                assert result is None

    def test_trace_context_takes_precedence(self):
        with patch(
            "app.api.middleware.get_lane",
            return_value="feat-v2",
        ):
            with patch.dict("os.environ", {"LANE": "dev"}):
                result = current_lane()
                assert result == "feat-v2"

    def test_trace_import_failure_falls_back_to_env(self):
        """If trace module raises, falls back to LANE env var."""
        with patch(
            "app.infra.rabbitmq.current_lane",
            side_effect=None,
        ):
            # Simulate import failure by making get_lane raise
            with patch(
                "app.api.middleware.get_lane",
                side_effect=ImportError("no trace"),
            ):
                with patch.dict("os.environ", {"LANE": "staging"}):
                    result = current_lane()
                    assert result == "staging"

    def test_empty_lane_env_returns_none(self):
        with patch(
            "app.api.middleware.get_lane",
            return_value=None,
        ):
            with patch.dict("os.environ", {"LANE": ""}):
                result = current_lane()
                assert result is None


# ---------------------------------------------------------------------------
# Route constants & ALL_ROUTES
# ---------------------------------------------------------------------------
class TestRouteConstants:
    """Verify the pre-defined route constants."""

    def test_route_is_namedtuple(self):
        assert isinstance(CHAT_REQUEST, Route)
        assert CHAT_REQUEST.queue == "chat_request"
        assert CHAT_REQUEST.rk == "chat.request"

    def test_all_routes_complete(self):
        expected = {
            CHAT_REQUEST,
            CHAT_RESPONSE,
            RECALL,
            VECTORIZE,
            MEMORY_FRAGMENT_VECTORIZE,
            MEMORY_ABSTRACT_VECTORIZE,
        }
        assert set(ALL_ROUTES) == expected

    def test_all_routes_have_six_entries(self):
        assert len(ALL_ROUTES) == 6

    def test_each_route_has_queue_and_rk(self):
        for route in ALL_ROUTES:
            assert route.queue, f"Route {route} has empty queue"
            assert route.rk, f"Route {route} has empty rk"
            # queue names use underscores, rk uses dots
            assert "_" not in route.rk or "." in route.rk
            assert "." not in route.queue

    def test_no_duplicate_queues(self):
        queues = [r.queue for r in ALL_ROUTES]
        assert len(queues) == len(set(queues))

    def test_no_duplicate_routing_keys(self):
        rks = [r.rk for r in ALL_ROUTES]
        assert len(rks) == len(set(rks))


# ---------------------------------------------------------------------------
# Route.lane_fallback + _build_queue_args(lane_fallback=...) — Phase 3 Task 1
# ---------------------------------------------------------------------------
def test_build_queue_args_prod_ignores_lane_fallback():
    args = _build_queue_args("rk", lane=None, lane_fallback=True)
    assert args == {"x-dead-letter-exchange": DLX_NAME}
    args2 = _build_queue_args("rk", lane=None, lane_fallback=False)
    assert args2 == {"x-dead-letter-exchange": DLX_NAME}


def test_build_queue_args_lane_with_fallback_default():
    args = _build_queue_args("rk", lane="dev", lane_fallback=True)
    assert args == {
        "x-message-ttl": _LANE_FALLBACK_TTL_MS,
        "x-dead-letter-exchange": EXCHANGE_NAME,
        "x-dead-letter-routing-key": "rk",
        "x-expires": _NON_PROD_EXPIRES_MS,
    }


def test_build_queue_args_lane_fallback_off_keeps_dlx():
    args = _build_queue_args("rk", lane="dev", lane_fallback=False)
    assert args == {
        "x-dead-letter-exchange": DLX_NAME,
        "x-expires": _NON_PROD_EXPIRES_MS,
    }
    assert "x-message-ttl" not in args
    assert "x-dead-letter-routing-key" not in args


def test_route_default_lane_fallback_true():
    r = Route("q", "rk")
    assert r.lane_fallback is True


def test_route_explicit_lane_fallback_false():
    r = Route("q", "rk", lane_fallback=False)
    assert r.lane_fallback is False


# ---------------------------------------------------------------------------
# declare_route / _ensure_lane_queue read route.lane_fallback — Phase 3 Task 2
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_declare_route_passes_lane_fallback_through(monkeypatch):
    """declare_route 应该把 route.lane_fallback 透传给 _build_queue_args。"""
    from app.infra.rabbitmq import _RabbitMQ

    mq = _RabbitMQ()
    mq._channel = MagicMock()
    mq._exchange = MagicMock()
    declared_args: dict[str, dict] = {}

    async def fake_declare_queue(name, durable, arguments):
        declared_args[name] = arguments
        q = MagicMock()
        q.bind = AsyncMock()
        return q

    mq._channel.declare_queue = AsyncMock(side_effect=fake_declare_queue)

    monkeypatch.setattr("app.infra.rabbitmq.current_lane", lambda: "dev")
    route = Route("q", "rk", lane_fallback=False)
    await mq.declare_route(route)

    args = declared_args["q_dev"]
    assert "x-message-ttl" not in args
    assert "x-dead-letter-routing-key" not in args
    assert args["x-dead-letter-exchange"] == DLX_NAME


@pytest.mark.asyncio
async def test_ensure_lane_queue_passes_lane_fallback_through_and_caches():
    """_ensure_lane_queue (lazy declare 路径，debounce publish 实际走这里)
    应该把 route.lane_fallback 透传给 _build_queue_args，且二次调用走 cache。"""
    from app.infra.rabbitmq import _RabbitMQ

    mq = _RabbitMQ()
    mq._channel = MagicMock()
    mq._exchange = MagicMock()
    declared_args: dict[str, dict] = {}

    async def fake_declare_queue(name, durable, arguments):
        declared_args[name] = arguments
        q = MagicMock()
        q.bind = AsyncMock()
        return q

    mq._channel.declare_queue = AsyncMock(side_effect=fake_declare_queue)

    route = Route("q", "rk", lane_fallback=False)
    await mq._ensure_lane_queue(route, lane="dev")

    args = declared_args["q_dev"]
    assert "x-message-ttl" not in args
    assert "x-dead-letter-routing-key" not in args
    assert args["x-dead-letter-exchange"] == DLX_NAME

    # cache_key 已记录
    assert "q_dev" in mq._declared_lane_queues

    # 二次调用短路：declare_queue 调用次数仍为 1
    await mq._ensure_lane_queue(route, lane="dev")
    assert mq._channel.declare_queue.await_count == 1
