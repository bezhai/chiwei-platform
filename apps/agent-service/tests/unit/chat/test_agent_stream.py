"""voice 子系统拆除后的 chat 上下文契约。

chat 的语气由主模型从 persona + life 此刻状态（inner_context 注入）现场生成，
不再读 reply_style_log 预排练的 <voice> 锚点。这里锁死 agent_stream 不再有
任何 voice 注入面。
"""
from __future__ import annotations

import inspect

import pytest


def test_agent_stream_module_free_of_voice_tokens():
    """agent_stream 源码不得再出现 voice 注入相关符号。"""
    import app.chat.agent_stream as m

    src = inspect.getsource(m)
    for token in ("voice_content", "find_latest_reply_style", "<voice>"):
        assert token not in src, f"voice residue in agent_stream: {token}"


@pytest.mark.asyncio
async def test_load_bot_context_has_no_voice(monkeypatch):
    """_load_bot_context 只装 persona，不再查 reply style / 携带 voice_content。"""
    from app.chat import agent_stream

    class _FakePersona:
        display_name = "赤尾"
        persona_lite = "lite"
        appearance_detail = "appearance"
        error_messages = {}

    async def _fake_load_persona(pid):
        return _FakePersona()

    monkeypatch.setattr(agent_stream, "load_persona", _fake_load_persona)

    ctx = await agent_stream._load_bot_context(
        persona_id="akao", bot_name="", chat_id="c1", chat_type="p2p"
    )

    assert ctx.persona_id == "akao"
    assert ctx.identity == "lite"
    assert ctx.appearance == "appearance"
    assert not hasattr(ctx, "voice_content")
