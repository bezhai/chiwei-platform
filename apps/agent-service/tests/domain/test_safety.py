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


def test_post_safety_request_adopts_agent_responses():
    """PostSafetyRequest 用 adoption mode adopt agent_responses 表。"""
    meta = getattr(PostSafetyRequest, "Meta", None)
    assert meta is not None
    assert getattr(meta, "existing_table", None) == "agent_responses"
    assert getattr(meta, "dedup_column", None) == "session_id"
    # 不能 transient（durable wire 要 row）
    assert getattr(meta, "transient", False) is False


def test_post_safety_request_key_is_session_id():
    assert key_fields(PostSafetyRequest) == ("session_id",)


def test_post_safety_request_required_fields():
    req = PostSafetyRequest(
        session_id="s1",
        trigger_message_id="m1",
        chat_id="c1",
        response_text="hello",
    )
    assert req.session_id == "s1"
    assert req.response_text == "hello"


def test_recall_is_transient():
    meta = getattr(Recall, "Meta", None)
    assert meta is not None
    assert getattr(meta, "transient", False) is True


def test_recall_key_is_session_id():
    assert key_fields(Recall) == ("session_id",)


def test_recall_lane_optional():
    """lane 可选（lark-server recall-worker 从 payload.lane 读，必须支持显式 None / str）。"""
    r = Recall(
        session_id="s1", chat_id="c1", trigger_message_id="m1",
        reason="banned_word",
    )
    assert r.lane is None
    r2 = Recall(
        session_id="s1", chat_id="c1", trigger_message_id="m1",
        reason="banned_word", lane="dev",
    )
    assert r2.lane == "dev"


def test_recall_serialization_matches_legacy_schema():
    """Recall.model_dump 字段集与旧 mq.publish(RECALL,...) 保持一致。"""
    r = Recall(
        session_id="s1", chat_id="c1", trigger_message_id="m1",
        reason="banned_word", detail="hit", lane="dev",
    )
    body = r.model_dump(mode="json")
    assert set(body.keys()) == {
        "session_id", "chat_id", "trigger_message_id",
        "reason", "detail", "lane",
    }


def test_data_class_extra_forbid():
    """Data 类应 frozen=True extra=forbid（pydantic Data base 行为）。"""
    with pytest.raises(ValidationError):
        PreSafetyRequest(
            pre_request_id="r1", message_id="m1",
            message_content="hi", persona_id="p1",
            unknown_field="x",
        )
