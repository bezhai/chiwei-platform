"""chat 完成后的 life 唤醒契约。

chat 内容不再回灌进 EventEnvelope 信箱。life 醒来时会实时从 common_message 拉
「最近聊过的对话」。chat_node 的事后副作用只剩：私聊完成后纯 emit EventArrived
叫醒对应 persona；群聊必须过白名单；白名单外群不唤醒。
"""

from __future__ import annotations

import pytest

from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment
from app.domain.world_events import EventArrived


def _happy_path_mocks(cn, monkeypatch, *, user_msg="input", reply_parts=None):
    """装上 chat_node happy-path 跑通所需的最小 mock。"""
    if reply_parts is None:
        reply_parts = ["hello"]

    async def fake_find_msg(mid):
        return user_msg

    async def fake_find_gray(mid):
        return {}

    async def fake_guard(p):
        return "guard"

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict

        return PreSafetyVerdict(
            pre_request_id="x", message_id="m1", is_blocked=False
        )

    from app.chat.render import ChatTurnContext

    async def fake_build_ctx(message_id, *, persona_id, **k):
        return ChatTurnContext(
            messages=[],
            image_registry=None,
            chat_id="c1",
            persona_id=persona_id,
            identity="",
            appearance="",
            inner_context="",
            persona=None,
        )

    async def fake_stream(*a, **k):
        for part in reply_parts:
            yield part

    async def fake_resolve(p, c):
        return "bot-x"

    async def fake_set(*a, **k):
        pass

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "build_human_chat_context", fake_build_ctx)
    monkeypatch.setattr(cn, "render_chat_turn", fake_stream)


def _request(*, is_p2p: bool, chat_id="chat-1") -> ChatRequest:
    return ChatRequest(
        message_id="m1",
        persona_id="akao",
        session_id="s1",
        chat_id=chat_id,
        is_p2p=is_p2p,
        user_id="u1",
        lane="coe-t1",
    )


@pytest.mark.asyncio
async def test_p2p_chat_wakes_life_without_whitelist(monkeypatch):
    """真人私聊完成后纯唤醒 life，且不查群白名单。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)

    async def should_not_be_called(**kwargs):
        raise AssertionError("p2p path must not consult group whitelist")

    emitted = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(cn, "should_feed_chat_to_life", should_not_be_called)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(_request(is_p2p=True))

    assert any(isinstance(e, ChatResponseSegment) for e in emitted)
    knocks = [e for e in emitted if isinstance(e, EventArrived)]
    assert len(knocks) == 1
    assert knocks[0].persona_id == "akao"


@pytest.mark.asyncio
async def test_whitelisted_group_chat_wakes_life_after_reply(monkeypatch):
    """白名单群聊完成后纯 emit EventArrived，lane 取进程部署泳道。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    monkeypatch.setenv("LANE", "coe-t1")
    whitelist_calls = []
    emitted = []

    async def fake_should_feed(*, chat_id, is_p2p):
        whitelist_calls.append((chat_id, is_p2p))
        return True

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(cn, "should_feed_chat_to_life", fake_should_feed)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(_request(is_p2p=False, chat_id="group-1"))

    assert whitelist_calls == [("group-1", False)]
    knocks = [e for e in emitted if isinstance(e, EventArrived)]
    assert len(knocks) == 1
    assert knocks[0].lane == "coe-t1"
    assert knocks[0].persona_id == "akao"
    assert any(isinstance(e, ChatResponseSegment) for e in emitted)


@pytest.mark.asyncio
async def test_non_whitelisted_group_chat_does_not_wake_life(monkeypatch):
    """白名单外群聊只回复，不叫醒 life。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    emitted = []

    async def fake_should_feed(*, chat_id, is_p2p):
        return False

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(cn, "should_feed_chat_to_life", fake_should_feed)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(_request(is_p2p=False, chat_id="group-2"))

    assert any(isinstance(e, ChatResponseSegment) for e in emitted)
    assert not any(isinstance(e, EventArrived) for e in emitted)


@pytest.mark.asyncio
async def test_group_wake_failure_does_not_fail_chat(monkeypatch):
    """群聊纯唤醒失败只记 warning，不拖垮已经发出的 chat 回复。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    emitted_segments = []

    async def fake_should_feed(*, chat_id, is_p2p):
        return True

    async def fake_emit(data):
        if isinstance(data, EventArrived):
            raise RuntimeError("redis unavailable")
        emitted_segments.append(data)

    monkeypatch.setattr(cn, "should_feed_chat_to_life", fake_should_feed)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(_request(is_p2p=False, chat_id="group-3"))

    assert any(isinstance(e, ChatResponseSegment) for e in emitted_segments)
