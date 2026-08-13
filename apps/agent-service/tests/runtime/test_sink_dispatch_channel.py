"""Sink dispatch 按消息的 channel 选 routing key。

出站队列按 channel 分区之后，agent-service 是唯一的生产者 —— rk 分对了是两个消费
服务不互相抢消息的前提。分流错了消费侧的 fail-closed 会拒收（并告警），但那已经是
第二道防线；第一道在这里。
"""

from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime import Data, Key, Sink, wire
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring

# Module-level Data classes so @node's get_type_hints() can resolve annotations.


class _ChannelSegment(Data):
    message_id: Annotated[str, Key]
    channel: str = "lark"

    class Meta:
        transient = True


class _ChannelRecall(Data):
    session_id: Annotated[str, Key]
    channel: str = "lark"

    class Meta:
        transient = True


class _NoChannelTrigger(Data):
    """channel 无关的队列上照常直投，不该被分区逻辑碰到。"""

    key: Annotated[str, Key]

    class Meta:
        transient = True


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    monkeypatch.setenv("APP_NAME", "agent-service")
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


async def _published_route(data: Data):
    fake_publish = AsyncMock()
    with patch("app.runtime.sink_dispatch.mq.publish", fake_publish):
        await emit(data)
    assert fake_publish.await_count == 1
    args, _kwargs = fake_publish.await_args
    return args[0]


@pytest.mark.asyncio
async def test_chat_response_goes_to_the_lark_queue():
    wire(_ChannelSegment).to(Sink.mq("chat_response"))

    route = await _published_route(_ChannelSegment(message_id="m1", channel="lark"))

    assert route.queue == "chat_response_lark"
    assert route.rk == "chat.response.lark"


@pytest.mark.asyncio
async def test_same_sink_different_channel_lands_on_a_different_queue():
    wire(_ChannelSegment).to(Sink.mq("chat_response"))

    lark = await _published_route(_ChannelSegment(message_id="m1", channel="lark"))
    clear_wiring()
    reset_emit_runtime()
    wire(_ChannelSegment).to(Sink.mq("chat_response"))
    qq = await _published_route(_ChannelSegment(message_id="m1", channel="qq"))

    assert (lark.queue, lark.rk) == ("chat_response_lark", "chat.response.lark")
    assert (qq.queue, qq.rk) == ("chat_response_qq", "chat.response.qq")


@pytest.mark.asyncio
async def test_recall_is_partitioned_the_same_way():
    wire(_ChannelRecall).to(Sink.mq("recall"))

    route = await _published_route(_ChannelRecall(session_id="s1", channel="qq"))

    assert route.queue == "recall_qq"
    assert route.rk == "action.recall.qq"


@pytest.mark.asyncio
async def test_unknown_channel_raises_instead_of_publishing_into_the_void():
    # 未注册的 channel 没有队列绑定，照发就是静默丢消息。
    wire(_ChannelSegment).to(Sink.mq("chat_response"))

    fake_publish = AsyncMock()
    with patch("app.runtime.sink_dispatch.mq.publish", fake_publish):
        with pytest.raises(ValueError, match="wechat"):
            await emit(_ChannelSegment(message_id="m1", channel="wechat"))

    assert fake_publish.await_count == 0


@pytest.mark.asyncio
async def test_non_partitioned_queue_is_untouched():
    wire(_NoChannelTrigger).to(Sink.mq("runtime_delayed_trigger_agent-service"))

    route = await _published_route(_NoChannelTrigger(key="k1"))

    assert route.queue == "runtime_delayed_trigger_agent-service"
    assert route.rk == "runtime.delayed_trigger.agent-service"


@pytest.mark.asyncio
async def test_lane_stays_on_the_channel_queue():
    # header 的 lane 与队列的 lane 必须同源；加了 channel 维度不改这条。
    wire(_ChannelSegment).to(Sink.mq("chat_response"))

    fake_publish = AsyncMock()
    with patch("app.runtime.sink_dispatch.mq.publish", fake_publish):
        await emit(_ChannelSegment(message_id="m1", channel="lark"))

    _args, kwargs = fake_publish.await_args
    assert "lane" in kwargs
