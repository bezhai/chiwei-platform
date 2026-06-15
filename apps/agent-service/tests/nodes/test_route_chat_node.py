"""route_chat_node 单元测试（Task 4-6 累积）。"""

import pytest

from app.domain.chat_dataflow import ChatTrigger


@pytest.mark.asyncio
async def test_route_chat_node_raises_on_missing_message_id():
    """缺 message_id -> raise，不静默 fan-out 空 ChatRequest。"""
    from app.nodes.chat_node import route_chat_node

    t = ChatTrigger()  # 全部默认值，message_id=None
    with pytest.raises((ValueError, AssertionError)):
        await route_chat_node(t)


@pytest.mark.asyncio
async def test_route_chat_node_short_circuits_when_completed(monkeypatch):
    """is_chat_request_completed 返 True -> 直接 return，不 emit。"""
    from app.nodes import chat_node as chat_node_mod
    from app.runtime.emit import reset_emit_runtime

    reset_emit_runtime()
    seen = []
    captured = {}

    async def fake_completed(session_id):
        captured["session_id"] = session_id
        return True

    async def fake_emit(*a, **k):
        seen.append((a, k))

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)

    t = ChatTrigger(message_id="m1", session_id="s1")
    await chat_node_mod.route_chat_node(t)

    assert captured == {"session_id": "s1"}
    assert seen == []  # 被 short-circuit


@pytest.mark.asyncio
async def test_route_chat_node_runs_router_when_not_completed(monkeypatch):
    """is_chat_request_completed 返 False -> 继续往下跑（验证至少不抛）。"""
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(session_id):
        return False

    async def fake_emit(*a, **k):
        pass

    class _FakeRouter:
        async def route(self, **kw):
            return []

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _FakeRouter())

    t = ChatTrigger(message_id="m1", session_id="s1")
    await chat_node_mod.route_chat_node(t)  # 不抛异常即可


@pytest.mark.asyncio
async def test_route_chat_node_single_persona_passes_session_id(monkeypatch):
    from app.domain.chat_dataflow import ChatRequest
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(*a, **k):
        return False

    class _Router:
        async def route(self, **kw):
            return ["p1"]

    emitted: list[ChatRequest] = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _Router())
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)

    t = ChatTrigger(
        message_id="m1",
        session_id="s1",
        chat_id="c1",
        bot_name="bot-x",
        lane="dev",
        is_p2p=True,
    )
    await chat_node_mod.route_chat_node(t)

    assert len(emitted) == 1
    r = emitted[0]
    assert r.message_id == "m1"
    assert r.persona_id == "p1"
    assert r.session_id == "s1"  # 第 1 个 persona 透传
    assert r.chat_id == "c1"
    assert r.lane == "dev"
    assert r.bot_name == "bot-x"
    assert r.is_p2p is True


@pytest.mark.asyncio
async def test_route_chat_node_multi_persona_regenerates_session_id(monkeypatch):
    from app.domain.chat_dataflow import ChatRequest
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(*a, **k):
        return False

    class _Router:
        async def route(self, **kw):
            return ["p1", "p2", "p3"]

    emitted: list[ChatRequest] = []
    pending_rows: list[dict] = []

    async def fake_emit(data):
        emitted.append(data)

    async def fake_create_pending(**kwargs):
        pending_rows.append(kwargs)

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _Router())
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)
    monkeypatch.setattr(
        chat_node_mod,
        "create_pending_agent_response",
        fake_create_pending,
    )

    t = ChatTrigger(message_id="m1", session_id="s1", chat_id="c1", bot_name="bot-x")
    await chat_node_mod.route_chat_node(t)

    assert len(emitted) == 3
    assert emitted[0].session_id == "s1"
    # 第 2/3 个 persona 重生成 uuid，且互不相等
    assert emitted[1].session_id != "s1"
    assert emitted[2].session_id != "s1"
    assert emitted[1].session_id != emitted[2].session_id
    assert pending_rows == [
        {
            "session_id": emitted[1].session_id,
            "trigger_common_message_id": "m1",
            "common_conversation_id": "c1",
            "bot_name": "bot-x",
        },
        {
            "session_id": emitted[2].session_id,
            "trigger_common_message_id": "m1",
            "common_conversation_id": "c1",
            "bot_name": "bot-x",
        },
    ]


@pytest.mark.asyncio
async def test_route_chat_node_empty_persona_list_no_emit(monkeypatch):
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(*a, **k):
        return False

    class _Router:
        async def route(self, **kw):
            return []

    emitted = []

    async def fake_emit(d):
        emitted.append(d)

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _Router())
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)

    t = ChatTrigger(message_id="m1", session_id="s1")
    await chat_node_mod.route_chat_node(t)
    assert emitted == []


@pytest.mark.asyncio
async def test_route_chat_node_propagates_channel(monkeypatch):
    """channel 必须从 ChatTrigger 透传到 fan-out 的 ChatRequest。
    缺失则非 lark 渠道被 Data 默认值静默改回 lark，多渠道直接击穿。
    """
    from app.domain.chat_dataflow import ChatRequest
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(*a, **k):
        return False

    class _Router:
        async def route(self, **kw):
            return ["p1"]

    emitted: list[ChatRequest] = []

    async def fake_emit(d):
        emitted.append(d)

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _Router())
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)

    t = ChatTrigger(message_id="m1", session_id="s1", channel="qq")
    await chat_node_mod.route_chat_node(t)

    assert len(emitted) == 1
    assert emitted[0].channel == "qq"
