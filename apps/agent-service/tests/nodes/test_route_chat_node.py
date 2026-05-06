"""route_chat_node 单元测试（Task 4-6 累积）。"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.chat_dataflow import ChatTrigger


def _fake_get_session_factory():
    """构造 ``get_session()`` 的 fake：MagicMock(return_value=async_ctx)。

    与 tests/nodes/test_safety.py 中同 pattern：``async with get_session() as s``
    实际只触发 ``__aenter__/__aexit__``；fake_session 自身既是 ctx 也是 yielded
    session（patched query 不会真用它）。
    """
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None
    return MagicMock(return_value=fake_session)


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
    captured_kwargs = {}

    async def fake_completed(session, session_id, *, is_proactive=False):
        captured_kwargs["session_id"] = session_id
        captured_kwargs["is_proactive"] = is_proactive
        return True

    async def fake_emit(*a, **k):
        seen.append((a, k))

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)
    monkeypatch.setattr(chat_node_mod, "get_session", _fake_get_session_factory())

    t = ChatTrigger(message_id="m1", session_id="s1", is_proactive=True)
    await chat_node_mod.route_chat_node(t)

    assert captured_kwargs == {"session_id": "s1", "is_proactive": True}
    assert seen == []  # 被 short-circuit


@pytest.mark.asyncio
async def test_route_chat_node_runs_router_when_not_completed(monkeypatch):
    """is_chat_request_completed 返 False -> 继续往下跑（验证至少不抛）。"""
    from app.nodes import chat_node as chat_node_mod

    async def fake_completed(session, session_id, *, is_proactive=False):
        return False

    async def fake_emit(*a, **k):
        pass

    class _FakeRouter:
        async def route(self, **kw):
            return []

    monkeypatch.setattr(chat_node_mod, "is_chat_request_completed", fake_completed)
    monkeypatch.setattr(chat_node_mod, "emit", fake_emit)
    monkeypatch.setattr(chat_node_mod, "MessageRouter", lambda: _FakeRouter())
    monkeypatch.setattr(chat_node_mod, "get_session", _fake_get_session_factory())

    t = ChatTrigger(message_id="m1", session_id="s1")
    await chat_node_mod.route_chat_node(t)  # 不抛异常即可
