"""Tests for app.infra.rabbitmq pure functions and constants."""

from __future__ import annotations

from unittest.mock import patch

from app.infra.rabbitmq import (
    ALL_ROUTES,
    CHAT_REQUEST,
    CHAT_RESPONSE,
    DLX_NAME,
    EXCHANGE_NAME,
    MEMORY_ABSTRACT_VECTORIZE,
    MEMORY_FRAGMENT_VECTORIZE,
    RECALL,
    SAFETY_CHECK,
    Route,
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
            SAFETY_CHECK,
            RECALL,
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
