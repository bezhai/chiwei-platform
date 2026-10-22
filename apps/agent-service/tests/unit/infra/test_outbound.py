"""What an outbound publish puts on the wire: for her words (``ChatResponseSegment`` →
``chat_response``) and what she takes back (``Recall`` → ``recall``), the queue, the routing key
(picked by the instance's own channel), the headers and the body.

The expected values are what the dataflow sink dispatch (``wire(T).to(Sink.mq(q))`` + ``emit``)
published before the host took outbound over; they are written out literally here, so a change in
any of them fails.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.domain.chat_dataflow import ChatResponseSegment
from app.domain.safety import Recall
from app.infra import outbound
from app.infra.propagation import Context, bind_context
from app.infra.rabbitmq import Route


@pytest.fixture(autouse=True)
def _registered(monkeypatch):
    monkeypatch.delenv("LANE", raising=False)
    undo = [
        outbound.register(ChatResponseSegment, "chat_response"),
        outbound.register(Recall, "recall"),
    ]
    yield
    for take_back in undo:
        take_back()


async def _published(data, *, bound: Context | None = None):
    sent = AsyncMock()
    with patch("app.infra.outbound.mq.publish", sent):
        if bound is None:
            await outbound.publish(data)
        else:
            async with bind_context(bound):
                await outbound.publish(data)
    assert sent.await_count == 1
    (route, body), kwargs = sent.await_args
    return route, body, kwargs["headers"], kwargs["lane"]


def _segment(**fields) -> ChatResponseSegment:
    return ChatResponseSegment(
        channel="lark",
        message_id="proactive:act-1:0",
        persona_id="akao",
        part_index=0,
        chat_id="chat-1",
        is_proactive=True,
        bot_name="akao-bot",
        content="在呢。",
        **fields,
    )


async def test_her_words_go_to_her_channels_queue_with_the_bound_trace_and_lane():
    route, body, headers, lane = await _published(
        _segment(lane="coe-living"), bound=Context(trace_id="t-1", lane="coe-living")
    )

    assert route == Route("chat_response_lark", "chat.response.lark")
    assert body == _segment(lane="coe-living").model_dump(mode="json")
    assert headers == {
        "data_type": "ChatResponseSegment",
        "trace_id": "t-1",
        "lane": "coe-living",
    }
    assert lane == "coe-living"


async def test_without_a_bound_lane_the_instances_own_lane_is_the_headers_and_the_queues():
    route, _body, headers, lane = await _published(_segment(lane="coe-living"))

    assert route == Route("chat_response_lark", "chat.response.lark")
    assert headers == {
        "data_type": "ChatResponseSegment",
        "trace_id": "",
        "lane": "coe-living",
    }
    assert lane == "coe-living"


async def test_with_no_lane_anywhere_it_goes_to_prod():
    _route, _body, headers, lane = await _published(_segment())

    assert headers["lane"] == ""
    assert lane is None


async def test_what_she_takes_back_goes_to_the_recall_queue_of_its_channel():
    recall = Recall(
        outbound_id="out-1",
        chat_id="chat-1",
        reason="她撤回了",
        channel="lark",
        lane="coe-living",
    )

    route, body, headers, lane = await _published(recall)

    assert route == Route("recall_lark", "action.recall.lark")
    assert body == recall.model_dump(mode="json")
    assert headers == {"data_type": "Recall", "trace_id": "", "lane": "coe-living"}
    assert lane == "coe-living"


async def test_another_channel_lands_on_its_own_queue():
    route, _body, _headers, _lane = await _published(
        _segment().model_copy(update={"channel": "qq"})
    )

    assert route == Route("chat_response_qq", "chat.response.qq")


async def test_a_queue_that_is_not_partitioned_is_published_to_as_it_is():
    from pydantic import BaseModel

    class Trigger(BaseModel):
        key: str

    take_back = outbound.register(Trigger, "chat_response_lark")
    try:
        route, body, headers, _lane = await _published(Trigger(key="k1"))
    finally:
        take_back()

    assert route == Route("chat_response_lark", "chat.response.lark")
    assert body == {"key": "k1"}
    assert headers["data_type"] == "Trigger"


async def test_a_channel_no_route_knows_is_refused_not_published():
    sent = AsyncMock()
    with patch("app.infra.outbound.mq.publish", sent), pytest.raises(ValueError):
        await outbound.publish(_segment().model_copy(update={"channel": "fax"}))
    assert sent.await_count == 0


async def test_a_type_registered_to_no_queue_is_refused():
    class Unwired(Recall):
        pass

    with pytest.raises(LookupError):
        await outbound.publish(
            Unwired(chat_id="c", reason="r", channel="lark", outbound_id="o")
        )


def test_a_queue_must_be_a_declared_route_and_a_partitioned_one_needs_a_channel():
    from pydantic import BaseModel

    class NoChannel(BaseModel):
        text: str

    with pytest.raises(ValueError):
        outbound.register(Recall, "no_such_queue")
    with pytest.raises(ValueError):
        outbound.register(NoChannel, "chat_response")
    with pytest.raises(ValueError):
        outbound.register(Recall, "recall")  # already registered


def test_taking_a_registration_back_unregisters_the_type():
    class Later(Recall):
        pass

    take_back = outbound.register(Later, "recall")
    take_back()

    assert Later not in outbound.registered()
