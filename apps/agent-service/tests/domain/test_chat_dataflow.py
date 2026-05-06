"""ChatTrigger / ChatRequest / ChatResponseSegment Data 类字段合约。"""
from app.runtime.data import key_fields


def test_chat_trigger_has_message_id_key_and_is_transient():
    from app.domain.chat_dataflow import ChatTrigger
    assert "message_id" in key_fields(ChatTrigger)
    assert ChatTrigger.Meta.transient is True


def test_chat_trigger_optional_fields_default_none():
    from app.domain.chat_dataflow import ChatTrigger
    t = ChatTrigger(message_id="m1")
    assert t.session_id is None
    assert t.chat_id is None
    assert t.is_p2p is False
    assert t.user_id is None
    assert t.lane is None
    assert t.is_proactive is False
    assert t.bot_name is None
    assert t.mentions == []
    assert t.enqueued_at is None


def test_chat_trigger_message_id_can_be_none_for_validation_resilience():
    """lark-server 偶尔不带 message_id；Data 反序列化要能成功。"""
    from app.domain.chat_dataflow import ChatTrigger
    t = ChatTrigger()
    assert t.message_id is None


def test_chat_request_has_message_id_persona_id_keys_not_transient():
    from app.domain.chat_dataflow import ChatRequest
    keys = key_fields(ChatRequest)
    assert "message_id" in keys
    assert "persona_id" in keys
    assert getattr(ChatRequest.Meta, "transient", False) is False


def test_chat_request_has_lane_field():
    from app.domain.chat_dataflow import ChatRequest
    r = ChatRequest(message_id="m1", persona_id="p1")
    assert r.lane is None
    r2 = ChatRequest(message_id="m1", persona_id="p1", lane="dev")
    assert r2.lane == "dev"


def test_chat_response_segment_dedup_keys_and_lane():
    from app.domain.chat_dataflow import ChatResponseSegment
    keys = key_fields(ChatResponseSegment)
    assert "message_id" in keys
    assert "persona_id" in keys
    assert "part_index" in keys
    seg = ChatResponseSegment(message_id="m1", persona_id="p1", part_index=0)
    assert seg.lane is None
    assert seg.is_last is False
    assert seg.status == "success"
    assert seg.content == ""


def test_chat_response_segment_is_transient():
    from app.domain.chat_dataflow import ChatResponseSegment
    assert ChatResponseSegment.Meta.transient is True
