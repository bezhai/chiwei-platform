"""chat_response / recall 的 channel 分区路由。

出站队列此前只按 lane 分区，channel 只是 payload 里的一个字段。出站的 owner 按
channel 拆成两个服务之后，共用一条队列意味着 RabbitMQ 轮询把流量随机劈成两半 ——
分区维度必须跟所有权维度一致。

命名口径与 TS 侧（packages/ts-shared/src/mq/client.ts::channelRoute）必须逐字一致，
而"逐字一致"不能靠两边各写各的 expected：那样"实现和本地 expected 一起被改"或者
"CI 只跑了一侧"都会让两边同时变绿，失效的表现却是两个服务静默守着对方不知道的
队列。所以两侧读同一份向量：contracts/mq-channel-routes.json。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.infra.rabbitmq import (
    ALL_ROUTES,
    CHANNEL_PARTITIONED_ROUTES,
    CHAT_REQUEST,
    CHAT_RESPONSE,
    DLX_NAME,
    EXCHANGE_NAME,
    KNOWN_CHANNELS,
    RECALL,
    Route,
    _build_queue_args,
    _lane_rk,
    channel_route,
    channel_route_for,
    lane_queue,
)

# 两侧读的是同一份文件。TS 侧：packages/ts-shared/src/mq/channel-route.test.ts
CONTRACT_PATH = (
    Path(__file__).resolve().parents[5] / "contracts" / "mq-channel-routes.json"
)
CONTRACT: dict[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

# 契约里的 base key → 本模块的 Route 常量。找不到即说明常量被改名了。
_BASE_ROUTES: dict[str, Route] = {
    CHAT_RESPONSE.queue: CHAT_RESPONSE,
    RECALL.queue: RECALL,
}


def _base_route(key: str) -> Route:
    assert key in _BASE_ROUTES, (
        f"contract base_routes has {key!r} but this module exposes "
        f"{sorted(_BASE_ROUTES)}"
    )
    return _BASE_ROUTES[key]


def _case_id(case: dict[str, Any]) -> str:
    return case["name"]


class TestContractVectorIntegrity:
    """向量自身的完整性 —— 缩水或自相矛盾的向量测不出任何东西。"""

    def test_base_routes_match_this_module(self):
        for key, expected in CONTRACT["base_routes"].items():
            route = _base_route(key)
            assert {"queue": route.queue, "rk": route.rk} == expected

    def test_cases_cover_base_times_channel_times_lane(self):
        expected = {
            f"{base}|{channel}|{lane or 'prod'}"
            for base in CONTRACT["base_routes"]
            for channel in CONTRACT["channels"]
            for lane in CONTRACT["lanes"]
        }
        actual = {
            f"{c['base']}|{c['channel']}|{c['lane'] or 'prod'}"
            for c in CONTRACT["cases"]
        }
        assert actual == expected

    def test_lane_cases_dead_letter_to_their_own_channel_prod_rk(self):
        # 泳道队列 10s TTL 到期后按 x-dead-letter-routing-key 弹回 prod，必须弹到
        # **同 channel** 的 prod rk —— 弹到别的 channel 上，回复由别的渠道发出去。
        for case in CONTRACT["cases"]:
            if case["lane"] is None:
                continue
            prod = next(
                c
                for c in CONTRACT["cases"]
                if c["lane"] is None
                and c["base"] == case["base"]
                and c["channel"] == case["channel"]
            )
            args = case["expect"]["queue_args"]
            assert args["x-dead-letter-routing-key"] == prod["expect"]["rk"]
            assert args["x-dead-letter-exchange"] == CONTRACT["exchanges"]["main"]


class TestContractVectorPythonSide:
    """Python 侧实现对齐向量。"""

    @pytest.mark.parametrize("case", CONTRACT["cases"], ids=_case_id)
    def test_queue_and_rk(self, case: dict[str, Any]):
        route = channel_route(_base_route(case["base"]), case["channel"])
        assert lane_queue(route.queue, case["lane"]) == case["expect"]["queue"]
        assert _lane_rk(route.rk, case["lane"]) == case["expect"]["rk"]

    @pytest.mark.parametrize("case", CONTRACT["cases"], ids=_case_id)
    def test_queue_args(self, case: dict[str, Any]):
        route = channel_route(_base_route(case["base"]), case["channel"])
        args = _build_queue_args(route.rk, case["lane"], route.lane_fallback)
        assert args == case["expect"]["queue_args"]

    def test_exchange_names_match(self):
        assert CONTRACT["exchanges"]["main"] == EXCHANGE_NAME
        assert CONTRACT["exchanges"]["dead_letter"] == DLX_NAME

    def test_channels_match_known_channels(self):
        assert tuple(CONTRACT["channels"]) == KNOWN_CHANNELS

    def test_same_message_different_channel_lands_on_different_queue(self):
        first, second = CONTRACT["channels"][0], CONTRACT["channels"][1]
        assert (
            channel_route(CHAT_RESPONSE, first).queue
            != channel_route(CHAT_RESPONSE, second).queue
        )
        assert (
            channel_route(CHAT_RESPONSE, first).rk
            != channel_route(CHAT_RESPONSE, second).rk
        )

    def test_lane_fallback_inherited_from_base(self):
        assert channel_route(CHAT_RESPONSE, "lark").lane_fallback is True
        base_no_fallback = CHAT_RESPONSE._replace(lane_fallback=False)
        assert channel_route(base_no_fallback, "lark").lane_fallback is False


class TestKnownChannelsRegistry:
    """已知 channel 是一份显式清单，不动态发现 —— 队列没被声明是静默失败。"""

    def test_known_channels_is_explicit(self):
        assert KNOWN_CHANNELS == ("lark", "qq")

    def test_partitioned_queues_are_chat_response_and_recall(self):
        assert set(CHANNEL_PARTITIONED_ROUTES) == {"chat_response", "recall"}

    def test_every_channel_route_is_declared_in_all_routes(self):
        for base_queue, by_channel in CHANNEL_PARTITIONED_ROUTES.items():
            assert set(by_channel) == set(KNOWN_CHANNELS), base_queue
            for route in by_channel.values():
                assert route in ALL_ROUTES, f"{route.queue} would never be declared"

    def test_partitioned_base_names_stay_registered(self):
        # base 名是 Sink.mq("chat_response") / Sink.mq("recall") 的标识：不在
        # ALL_ROUTES 里 compile_graph 就会在启动时 GraphError，进程起不来。真实 rk
        # 由 dispatch 按 payload 的 channel 现算，跟这两条 Route 的 rk 无关。
        assert CHAT_RESPONSE in ALL_ROUTES
        assert RECALL in ALL_ROUTES


class TestChannelRouteFor:
    """fail-closed：未知 channel 直接抛，绝不投到一条没人声明的队列上。"""

    def test_resolves_registered_channel(self):
        assert channel_route_for("chat_response", "lark").rk == "chat.response.lark"
        assert channel_route_for("recall", "qq").queue == "recall_qq"

    def test_unknown_channel_raises_pointing_at_the_list(self):
        with pytest.raises(ValueError) as excinfo:
            channel_route_for("chat_response", "wechat")
        assert "wechat" in str(excinfo.value)
        assert "KNOWN_CHANNELS" in str(excinfo.value)

    def test_empty_channel_raises(self):
        with pytest.raises(ValueError):
            channel_route_for("chat_response", "")

    def test_queue_that_is_not_channel_partitioned_raises(self):
        with pytest.raises(ValueError) as excinfo:
            channel_route_for(CHAT_REQUEST.queue, "lark")
        assert "chat_request" in str(excinfo.value)
