"""Tests for safety Data classes (Phase 2)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.safety import (
    PostSafetyRequest,
    PreSafetyRequest,
    PreSafetyVerdict,
    Recall,
)
from app.runtime.data import key_fields


def test_pre_safety_request_is_transient():
    """PreSafetyRequest 是 transient（不落表）。"""
    meta = getattr(PreSafetyRequest, "Meta", None)
    assert meta is not None
    assert getattr(meta, "transient", False) is True


def test_pre_safety_request_key_is_pre_request_id():
    assert key_fields(PreSafetyRequest) == ("pre_request_id",)


def test_pre_safety_verdict_is_transient():
    meta = getattr(PreSafetyVerdict, "Meta", None)
    assert meta is not None
    assert getattr(meta, "transient", False) is True


def test_pre_safety_verdict_key_is_pre_request_id():
    assert key_fields(PreSafetyVerdict) == ("pre_request_id",)


def test_pre_safety_verdict_default_passes():
    """is_blocked 默认 False，block_reason 默认 None。"""
    v = PreSafetyVerdict(pre_request_id="r1", message_id="m1", is_blocked=False)
    assert v.is_blocked is False
    assert v.block_reason is None
    assert v.detail is None


def test_post_safety_request_is_transient():
    """PostSafetyRequest is a transient trigger; DB state is common_agent_response."""
    meta = getattr(PostSafetyRequest, "Meta", None)
    assert meta is not None
    assert getattr(meta, "transient", False) is True


def test_post_safety_request_key_is_session_id():
    assert key_fields(PostSafetyRequest) == ("session_id",)


def test_post_safety_request_required_fields():
    """channel 在必填字段之列，且原样带过来（不是被默认值填的）。"""
    req = PostSafetyRequest(
        session_id="s1",
        trigger_message_id="m1",
        chat_id="c1",
        response_text="hello",
        channel="qq",
    )
    assert req.session_id == "s1"
    assert req.channel == "qq"
    assert req.response_text == "hello"


def test_post_safety_request_channel_is_required():
    """channel 必填：缺了直接 ValidationError，不猜"lark"。

    出站队列按 channel 分区（chat_response_lark / chat_response_qq ...），
    一个默认值就能把别的渠道的回复静默投进飞书队列。sink dispatch 的
    fail-closed 在 pydantic 默认值之后生效，拦不住，只能在源头必填。
    """
    with pytest.raises(ValidationError):
        PostSafetyRequest(
            session_id="s1",
            trigger_message_id="m1",
            chat_id="c1",
            response_text="hello",
        )


def test_recall_is_transient():
    meta = getattr(Recall, "Meta", None)
    assert meta is not None
    assert getattr(meta, "transient", False) is True


def test_recall_key_is_session_id():
    assert key_fields(Recall) == ("session_id",)


def test_recall_lane_optional():
    """lane 可选（channel-server recall-worker 从 payload.lane 读，必须支持显式 None / str）。"""
    r = Recall(
        session_id="s1", chat_id="c1", trigger_message_id="m1",
        reason="banned_word", channel="qq",
    )
    assert r.lane is None
    r2 = Recall(
        session_id="s1", chat_id="c1", trigger_message_id="m1",
        reason="banned_word", channel="qq", lane="dev",
    )
    assert r2.channel == "qq"
    assert r2.lane == "dev"


def test_recall_channel_is_required():
    """channel 必填：撤回投的是 recall_{channel}，猜错就撤到别的渠道去了。"""
    with pytest.raises(ValidationError):
        Recall(
            session_id="s1",
            chat_id="c1",
            trigger_message_id="m1",
            reason="banned_word",
        )


def test_recall_serialization_carries_channel_for_pluginized_worker():
    """Recall.model_dump 字段集带 channel，recall-worker 按它取插件。"""
    r = Recall(
        session_id="s1", chat_id="c1", trigger_message_id="m1",
        reason="banned_word", channel="qq", detail="hit", lane="dev",
    )
    body = r.model_dump(mode="json")
    assert set(body.keys()) == {
        "session_id", "channel", "chat_id", "trigger_message_id",
        "reason", "detail", "lane",
    }
    assert body["channel"] == "qq"


def test_data_class_extra_forbid():
    """Data 类应 frozen=True extra=forbid（pydantic Data base 行为）。"""
    with pytest.raises(ValidationError):
        PreSafetyRequest(
            pre_request_id="r1", message_id="m1",
            message_content="hi", persona_id="p1",
            unknown_field="x",
        )
