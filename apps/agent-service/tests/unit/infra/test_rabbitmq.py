"""Tests for app.infra.rabbitmq pure functions and constants."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infra.rabbitmq import (
    ALL_ROUTES,
    CHAT_RESPONSE,
    DLX_NAME,
    EXCHANGE_NAME,
    RECALL,
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
        assert lane_queue("chat_response", None) == "chat_response"

    def test_lane_appends_suffix(self):
        assert lane_queue("chat_response", "dev") == "chat_response_dev"

    def test_lane_with_hyphen(self):
        assert lane_queue("recall", "feat-v2") == "recall_feat-v2"


class TestLaneRk:
    """_lane_rk appends lane as a dotted segment."""

    def test_prod_returns_base(self):
        assert _lane_rk("chat.response", None) == "chat.response"

    def test_lane_appends_dot_segment(self):
        assert _lane_rk("chat.response", "dev") == "chat.response.dev"

    def test_lane_with_hyphen(self):
        assert _lane_rk("action.recall", "feat-v2") == "action.recall.feat-v2"


# ---------------------------------------------------------------------------
# _build_queue_args
# ---------------------------------------------------------------------------
class TestBuildQueueArgs:
    """_build_queue_args returns correct DLX/TTL/expire args."""

    def test_prod_queue_has_dlx(self):
        args = _build_queue_args("chat.response", None)
        assert args["x-dead-letter-exchange"] == DLX_NAME

    def test_prod_queue_no_ttl(self):
        args = _build_queue_args("chat.response", None)
        assert "x-message-ttl" not in args

    def test_prod_queue_no_expires(self):
        args = _build_queue_args("chat.response", None)
        assert "x-expires" not in args

    def test_lane_queue_has_ttl(self):
        args = _build_queue_args("chat.response", "dev")
        assert args["x-message-ttl"] == 10_000

    def test_lane_queue_fallback_to_main_exchange(self):
        args = _build_queue_args("chat.response", "dev")
        assert args["x-dead-letter-exchange"] == EXCHANGE_NAME

    def test_lane_queue_fallback_rk_is_prod(self):
        """Dead-lettered messages route back to the prod routing key."""
        args = _build_queue_args("chat.response", "dev")
        assert args["x-dead-letter-routing-key"] == "chat.response"

    def test_lane_queue_auto_expires(self):
        args = _build_queue_args("chat.response", "dev")
        assert args["x-expires"] == 86_400_000

    def test_lane_queue_no_dlx_to_dlq(self):
        """Lane queues should NOT dead-letter to DLX (they fallback to prod)."""
        args = _build_queue_args("chat.response", "dev")
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
        assert isinstance(CHAT_RESPONSE, Route)
        assert CHAT_RESPONSE.queue == "chat_response"
        assert CHAT_RESPONSE.rk == "chat.response"

    def test_chat_request_is_gone(self):
        """「这条消息触发一次聊天请求」这个概念已经不存在了。

        赤尾不从队列拿消息，她每次醒来直接查 ``common_message``、自己决定要不要开口。
        所以 ``chat_request`` 既没有生产者也没有消费者 —— 它不该再出现在声明面
        （``declare_topology`` 遍历 ALL_ROUTES 建队列）或注册面（``Sink.mq`` 的合法
        队列名从 ALL_ROUTES 来）的任何一侧。
        """
        import app.infra.rabbitmq as rabbitmq

        assert not hasattr(rabbitmq, "CHAT_REQUEST")
        assert all(r.queue != "chat_request" for r in ALL_ROUTES)
        assert all(r.rk != "chat.request" for r in ALL_ROUTES)

    def test_all_routes_complete(self):
        # v4 vectorize 队列（memory_fragment_vectorize / memory_abstract_vectorize）
        # 随 v4 记忆整机删除。
        # chat_response / recall 的 channel 分区队列也在这里 —— declare_topology 只
        # 遍历 ALL_ROUTES，漏掉就等于队列压根没被声明，而声明缺失是静默的。
        # 两条 base 也在，理由不是"要声明"而是"Sink.mq 认这个名字"，见 rabbitmq.py。
        from app.infra.rabbitmq import CHANNEL_ROUTES

        expected = {
            CHAT_RESPONSE,
            RECALL,
            *CHANNEL_ROUTES,
        }
        assert set(ALL_ROUTES) == expected

    def test_all_routes_match_business_routes(self):
        # 2 business routes + 每个 channel-partitioned base × 每个已知 channel
        from app.infra.rabbitmq import CHANNEL_ROUTES

        assert len(ALL_ROUTES) == 2 + len(CHANNEL_ROUTES)

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
# declare_route / _ensure_lane_queue build the lane queue's arguments
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_declare_route_gives_the_lane_queue_its_fallback_to_prod(monkeypatch):
    """declare_route 声明的泳道队列带 TTL 回落 prod 的参数（_build_queue_args）。"""
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
    await mq.declare_route(Route("q", "rk"))

    assert declared_args["q_dev"] == _build_queue_args("rk", "dev")
    assert declared_args["q_dev"]["x-dead-letter-routing-key"] == "rk"


@pytest.mark.asyncio
async def test_ensure_lane_queue_declares_the_fallback_once_and_caches():
    """_ensure_lane_queue (lazy declare 路径) 声明带回落参数的泳道队列，二次调用走 cache。"""
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

    route = Route("q", "rk")
    await mq._ensure_lane_queue(route, lane="dev")

    assert declared_args["q_dev"] == _build_queue_args("rk", "dev")

    # cache_key 已记录
    assert "q_dev" in mq._declared_lane_queues

    # 二次调用短路：declare_queue 调用次数仍为 1
    await mq._ensure_lane_queue(route, lane="dev")
    assert mq._channel.declare_queue.await_count == 1


# ---------------------------------------------------------------------------
# Route.isolated —— 通信机制的收件箱用的那种队列
#
# 平台原有的泳道队列三件事对收件箱都是错的：没人消费 10 秒就转回 prod、闲置 24 小时
# 被删、死信全部泳道共用一条。isolated 队列把这三样全部关掉或者按泳道隔开，并且
# 只由拥有者声明——发送方永远不会顺带把它建出来。
# ---------------------------------------------------------------------------
class TestIsolatedQueueArgs:
    def test_lane_queue_never_falls_back_to_prod(self):
        args = _build_queue_args("inbox.world", "coe-x", isolated=True)
        assert "x-message-ttl" not in args
        assert args.get("x-dead-letter-routing-key") != "inbox.world"
        assert args["x-dead-letter-exchange"] != EXCHANGE_NAME

    def test_lane_queue_never_expires_when_idle(self):
        args = _build_queue_args("inbox.world", "coe-x", isolated=True)
        assert "x-expires" not in args

    def test_lane_queue_dead_letters_into_its_own_lane(self):
        from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS

        args = _build_queue_args("inbox.world", "coe-x", isolated=True)
        assert args == {
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": f"{ISOLATED_DEAD_LETTERS}_coe-x",
        }

    def test_prod_queue_does_not_share_the_common_dead_letter_queue(self):
        from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS

        args = _build_queue_args("inbox.world", None, isolated=True)
        assert args == {
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": ISOLATED_DEAD_LETTERS,
        }
        assert DLX_NAME not in args.values()


def _client_with_fake_channel():
    from app.infra.rabbitmq import _RabbitMQ

    client = _RabbitMQ()
    client._channel = MagicMock()
    client._exchange = MagicMock()
    client._exchange.publish = AsyncMock()
    declared: dict[str, dict | None] = {}

    async def fake_declare_queue(name, durable=True, arguments=None):
        declared[name] = arguments
        q = MagicMock()
        q.bind = AsyncMock()
        return q

    client._channel.declare_queue = AsyncMock(side_effect=fake_declare_queue)
    return client, declared


@pytest.mark.asyncio
async def test_publishing_never_creates_an_isolated_queue():
    """发送方不能顺带把收件箱建出来：那样"没开设"这件事就不存在了。"""
    client, declared = _client_with_fake_channel()
    route = Route("inbox_world", "inbox.world", isolated=True)

    assert await client.publish_with_confirm(route, {"a": 1}, lane="coe-x")
    await client.publish(route, {"a": 1}, lane="coe-x")

    assert declared == {}
    rks = [c.kwargs["routing_key"] for c in client._exchange.publish.await_args_list]
    assert rks == ["inbox.world.coe-x", "inbox.world.coe-x"]


@pytest.mark.asyncio
async def test_declaring_an_isolated_route_declares_its_lanes_dead_letter_queue():
    from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS

    client, declared = _client_with_fake_channel()
    route = Route("inbox_world", "inbox.world", isolated=True)

    await client.declare_route(route, lane="coe-x")

    assert declared[f"{ISOLATED_DEAD_LETTERS}_coe-x"] is None
    assert declared["inbox_world_coe-x"] == _build_queue_args(
        "inbox.world", "coe-x", isolated=True
    )


@pytest.mark.asyncio
async def test_declare_route_takes_an_explicit_lane():
    """拥有者开设收件箱时按进程自己的部署泳道，不看请求上下文。"""
    client, declared = _client_with_fake_channel()
    with patch("app.api.middleware.get_lane", return_value="ppe-other"):
        await client.declare_route(Route("q", "rk", isolated=True), lane="coe-x")
    assert "q_coe-x" in declared
    assert "q_ppe-other" not in declared


def test_x_delay_upper_bound_lives_with_the_broker_client():
    """x-delay 是 int32 毫秒，这个上限只在一处定义。"""
    import app.runtime.emit as emit_mod
    from app.infra.rabbitmq import X_DELAY_MAX_MS

    assert X_DELAY_MAX_MS == 2_147_483_647
    assert not hasattr(emit_mod, "_X_DELAY_MAX_MS")


def test_a_dead_lettered_isolated_message_knows_where_it_was_headed():
    """重放要把死信送回它原来去的那条队列：从 broker 加的 x-death 读，读不到就不猜。"""
    from app.infra.rabbitmq import dead_letter_origin

    headers = {
        "x-death": [
            {
                "count": 1,
                "reason": "rejected",
                "queue": "inbox_world_coe-x",
                "exchange": EXCHANGE_NAME,
                "routing-keys": ["inbox.world.coe-x"],
            }
        ],
        "x-first-death-queue": "inbox_world_coe-x",
    }
    origin = dead_letter_origin(headers)
    assert origin == Route("inbox_world_coe-x", "inbox.world.coe-x", isolated=True)
    assert dead_letter_origin({}) is None
    assert dead_letter_origin({"x-death": [{"queue": "q"}]}) is None
