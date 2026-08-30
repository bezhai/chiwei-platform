"""ChatTrigger / ChatRequest / ChatResponseSegment Data 类字段合约。"""
import pytest
from pydantic import ValidationError

from app.runtime.data import key_fields


def test_chat_trigger_has_message_id_key_and_is_transient():
    from app.domain.chat_dataflow import ChatTrigger
    assert "message_id" in key_fields(ChatTrigger)
    assert ChatTrigger.Meta.transient is True


def test_chat_trigger_optional_fields_default_none():
    from app.domain.chat_dataflow import ChatTrigger
    t = ChatTrigger(message_id="m1", channel="lark")
    assert t.session_id is None
    assert t.chat_id is None
    assert t.is_p2p is False
    assert t.user_id is None
    assert t.lane is None
    assert t.is_proactive is False
    assert t.bot_name is None
    assert t.persona_ids == []
    assert t.enqueued_at is None


def test_chat_trigger_message_id_can_be_none_for_validation_resilience():
    """channel-server 偶尔不带 message_id；Data 反序列化要能成功。"""
    from app.domain.chat_dataflow import ChatTrigger
    t = ChatTrigger(channel="lark")
    assert t.message_id is None


def test_chat_request_has_message_id_persona_id_keys_not_transient():
    from app.domain.chat_dataflow import ChatRequest
    keys = key_fields(ChatRequest)
    assert "message_id" in keys
    assert "persona_id" in keys
    assert getattr(ChatRequest.Meta, "transient", False) is False


def test_chat_request_has_lane_field():
    from app.domain.chat_dataflow import ChatRequest
    r = ChatRequest(message_id="m1", persona_id="p1", channel="lark")
    assert r.lane is None
    r2 = ChatRequest(
        message_id="m1", persona_id="p1", channel="lark", lane="dev"
    )
    assert r2.lane == "dev"


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


def test_chat_trigger_channel_is_required():
    from app.domain.chat_dataflow import ChatTrigger
    with pytest.raises(ValidationError):
        ChatTrigger(message_id="m1")
    assert ChatTrigger(message_id="m1", channel="qq").channel == "qq"


def test_chat_trigger_payload_without_channel_fails_validation():
    """缺 channel 的 chat_request 报文必须炸在反序列化，不能兜底成 lark。"""
    from app.domain.chat_dataflow import ChatTrigger
    old = {"message_id": "m1", "chat_id": "c1", "user_id": "u1"}
    with pytest.raises(ValidationError):
        ChatTrigger.model_validate(old)


def test_chat_request_channel_is_required():
    from app.domain.chat_dataflow import ChatRequest
    with pytest.raises(ValidationError):
        ChatRequest(message_id="m1", persona_id="p1")
    assert (
        ChatRequest(message_id="m1", persona_id="p1", channel="qq").channel == "qq"
    )


def test_chat_request_payload_without_channel_fails_validation():
    from app.domain.chat_dataflow import ChatRequest
    old = {"message_id": "m1", "persona_id": "p1", "chat_id": "c1"}
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(old)


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
