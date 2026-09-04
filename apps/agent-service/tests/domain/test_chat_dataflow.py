"""ChatResponseSegment Data 类字段合约。

ChatTrigger / ChatRequest 随 chat_request 队列一起删了：她不从队列拿消息，
每一缝直接查 ``common_message``、自己决定要不要开口（见 ``app.living``）。
这个模块现在只剩她开口那一侧的契约。
"""
import pytest
from pydantic import ValidationError

from app.runtime.data import key_fields


def test_chat_response_segment_dedup_keys_and_lane():
    from app.domain.chat_dataflow import ChatResponseSegment
    keys = key_fields(ChatResponseSegment)
    assert "message_id" in keys
    assert "persona_id" in keys
    assert "part_index" in keys
    seg = ChatResponseSegment(
        message_id="m1", persona_id="p1", part_index=0, channel="lark"
    )
    assert seg.lane is None
    assert seg.is_last is False
    assert seg.status == "success"
    assert seg.content == ""


def test_chat_response_segment_is_transient():
    from app.domain.chat_dataflow import ChatResponseSegment
    assert ChatResponseSegment.Meta.transient is True


# ---- channel 字段：必填，没有默认值 ----
# channel 决定出站 routing key（chat_response_{channel}）。给它默认值等于让
# 「漏传 channel」静默变成飞书：一条 QQ 消息会被记成 lark、回复投进飞书出站
# 队列，QQ 用户收不到回复，全程不报错。sink dispatch 的 fail-closed 校验跑在
# pydantic 填完默认值之后，拦不住。所以三个 Data 一律必填，缺就在反序列化处
# 报 ValidationError（MQ 入口 → 消息进 DLQ，可查），不猜。


def test_chat_response_segment_channel_is_required():
    from app.domain.chat_dataflow import ChatResponseSegment
    with pytest.raises(ValidationError):
        ChatResponseSegment(message_id="m1", persona_id="p1", part_index=0)
    assert ChatResponseSegment(
        message_id="m1", persona_id="p1", part_index=0, channel="qq"
    ).channel == "qq"


def test_chat_response_segment_payload_without_channel_fails_validation():
    from app.domain.chat_dataflow import ChatResponseSegment
    old = {"message_id": "m1", "persona_id": "p1", "part_index": 0, "content": "hi"}
    with pytest.raises(ValidationError):
        ChatResponseSegment.model_validate(old)


def test_the_chat_request_payload_contracts_are_gone():
    """``chat_request`` 那条队列的报文契约不该再存在。

    ``ChatTrigger`` 是它的入口 body，``ChatRequest`` 是 fan-out 之后的 per-persona
    请求。队列没了，这两条 Data 也就没有任何生产者和消费者 —— 留着的话，下一个人
    会以为"接一下就能用"，而那正是「chat 是嘴，没有耳朵」要挡住的东西
    （见 ``tests/living/test_no_inbound.py``）。
    """
    import app.domain.chat_dataflow as chat_dataflow

    assert not hasattr(chat_dataflow, "ChatTrigger")
    assert not hasattr(chat_dataflow, "ChatRequest")
