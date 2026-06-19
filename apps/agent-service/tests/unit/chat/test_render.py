"""共享渲染层 ``render_chat_turn`` 的契约。

渲染层吃一份**已构建好的** ChatTurnContext（含 LLM messages + persona bundle +
inner_context）+ 出站参数（outbound_message_id / session_id / channel / features），
用人设 prompt + 主模型 stream 生成文本并 yield 出来，最后跑 post-actions。

锁死三件事：
  1. 渲染层不依赖源消息 id 去**构建** context —— message_id 只是出站/trace 标识，
     由调用方透传（真人传真实 id，task 2 的 proactive 传派生 id）。
  2. 渲染层把 context 的 persona bundle + inner_context + 渲染期变量（available_skills /
     complexity_hint）拼成 prompt_vars 喂 Agent；features 决定模型覆盖。
  3. content_filter / length / 异常分别走 persona error / 截断提示 / persona error，
     与旧 _build_and_stream 行为一致；正常结束跑 schedule_post_actions。
"""

from __future__ import annotations

import inspect

import pytest

from app.agent.neutral import Message, Role, StreamChunk


def _ctx(**overrides):
    """构造一份最小的 ChatTurnContext（已构建好的 context + persona bundle）。"""
    from app.chat.render import ChatTurnContext

    class _FakePersona:
        display_name = "赤尾"
        error_messages = {"content_filter": "CF", "error": "ERR", "guard": "GUARD"}

    defaults = {
        "messages": [Message(role=Role.USER, content="hi")],
        "image_registry": None,
        "chat_id": "c1",
        "persona_id": "akao",
        "identity": "lite-body",
        "appearance": "looks",
        "inner_context": "inner-ctx",
        "reply_style": "few-shot 口吻样例",
        "persona": _FakePersona(),
    }
    defaults.update(overrides)
    return ChatTurnContext(**defaults)


def test_render_and_context_free_of_voice_tokens():
    """渲染层 + 真人 context 构建都不得残留 voice 注入符号（承接旧 agent_stream
    的 voice-free 锁,语气现场由主模型从 persona + life 此刻状态生成）。"""
    import app.chat.context as ctx_mod
    import app.chat.render as render_mod

    for m in (render_mod, ctx_mod):
        src = inspect.getsource(m)
        for token in ("voice_content", "find_latest_reply_style", "<voice>"):
            assert token not in src, f"voice residue in {m.__name__}: {token}"


def test_render_chat_turn_signature_has_no_source_message_lookup():
    """渲染层签名:吃 turn_ctx + 出站参数,绝不吃用来反查源消息的 message_id。"""
    from app.chat.render import render_chat_turn

    params = inspect.signature(render_chat_turn).parameters
    # 第一个位置参数是已构建好的 context
    names = list(params)
    assert names[0] == "turn_ctx"
    # 出站标识是 outbound_message_id(透传,不反查),不叫 message_id
    assert "outbound_message_id" in params
    assert "session_id" in params
    assert "channel" in params
    assert "features" in params
    # 渲染层不接受任何反查源消息的入参
    assert "message_id" not in params


@pytest.mark.asyncio
async def test_render_chat_turn_streams_text_and_builds_prompt_vars(monkeypatch):
    """正常路径:用 persona bundle + inner_context + 渲染期变量拼 prompt_vars,
    透传 outbound_message_id/chat_id 到 AgentContext,yield 出文本。"""
    from app.chat import render as render_mod

    captured = {}

    class _FakeAgent:
        def __init__(self, cfg, tools=None):
            captured["cfg"] = cfg

        async def stream(self, messages, *, context, prompt_vars):
            captured["messages"] = messages
            captured["context"] = context
            captured["prompt_vars"] = prompt_vars
            yield StreamChunk(text="你好")
            yield StreamChunk(text="呀")

    monkeypatch.setattr(render_mod, "Agent", _FakeAgent)

    class _FakeRegistry:
        @staticmethod
        def list_descriptions():
            return "SKILLS"

    monkeypatch.setattr(render_mod.SkillRegistry, "list_descriptions",
                        staticmethod(lambda: "SKILLS"), raising=False)

    posted = {}

    async def fake_post(**kwargs):
        posted.update(kwargs)

    monkeypatch.setattr(render_mod, "schedule_post_actions", fake_post)

    out = []
    async for text in render_mod.render_chat_turn(
        _ctx(),
        outbound_message_id="m-out",
        session_id="s1",
        channel="lark",
        features={"main_model": "override-model"},
    ):
        out.append(text)

    assert "".join(out) == "你好呀"
    # prompt_vars 来自 context 的 persona bundle + inner_context + 渲染期变量
    pv = captured["prompt_vars"]
    assert pv["identity"] == "lite-body"
    assert pv["appearance"] == "looks"
    assert pv["inner_context"] == "inner-ctx"
    assert pv["available_skills"] == "SKILLS"
    assert "complexity_hint" in pv
    # per-persona 说话风格 {{reply_style}} 来自 context，原样进 prompt_vars
    assert pv["reply_style"] == "few-shot 口吻样例"
    # features 决定模型覆盖
    assert captured["cfg"].model_id == "override-model"
    # AgentContext 透传 outbound id + chat_id,不反查源消息
    assert captured["context"].message_id == "m-out"
    assert captured["context"].chat_id == "c1"
    assert captured["context"].persona_id == "akao"
    # 正常结束跑 post-actions,透传 outbound id 作 trigger_message_id
    assert posted["message_id"] == "m-out"
    assert posted["session_id"] == "s1"
    assert posted["chat_id"] == "c1"
    assert posted["full_content"] == "你好呀"


@pytest.mark.asyncio
async def test_render_chat_turn_default_model_when_no_override(monkeypatch):
    """features 不带 main_model 时用默认 main-chat-model。"""
    from app.chat import render as render_mod

    captured = {}

    class _FakeAgent:
        def __init__(self, cfg, tools=None):
            captured["cfg"] = cfg

        async def stream(self, messages, *, context, prompt_vars):
            yield StreamChunk(text="x")

    monkeypatch.setattr(render_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(render_mod, "schedule_post_actions",
                        _noop_post)

    async for _ in render_mod.render_chat_turn(
        _ctx(), outbound_message_id="m", session_id=None, channel="lark", features={},
    ):
        pass

    assert captured["cfg"].model_id == "main-chat-model"


@pytest.mark.asyncio
async def test_render_chat_turn_content_filter_yields_persona_error(monkeypatch):
    """content_filter -> yield persona 的 content_filter 文案后停止。"""
    from app.chat import render as render_mod

    class _FakeAgent:
        def __init__(self, cfg, tools=None):
            pass

        async def stream(self, messages, *, context, prompt_vars):
            yield StreamChunk(finish_reason="content_filter")
            yield StreamChunk(text="不该出现")

    monkeypatch.setattr(render_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(render_mod, "schedule_post_actions", _noop_post)

    out = []
    async for text in render_mod.render_chat_turn(
        _ctx(), outbound_message_id="m", session_id=None, channel="lark", features={},
    ):
        out.append(text)

    assert out == ["CF"]


@pytest.mark.asyncio
async def test_render_chat_turn_length_yields_truncation(monkeypatch):
    """length -> yield 截断提示后停止。"""
    from app.chat import render as render_mod

    class _FakeAgent:
        def __init__(self, cfg, tools=None):
            pass

        async def stream(self, messages, *, context, prompt_vars):
            yield StreamChunk(text="部分")
            yield StreamChunk(finish_reason="length")

    monkeypatch.setattr(render_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(render_mod, "schedule_post_actions", _noop_post)

    out = []
    async for text in render_mod.render_chat_turn(
        _ctx(), outbound_message_id="m", session_id=None, channel="lark", features={},
    ):
        out.append(text)

    assert out[0] == "部分"
    assert "截断" in out[-1]


@pytest.mark.asyncio
async def test_render_chat_turn_stream_exception_yields_persona_error(monkeypatch):
    """stream 抛异常 -> yield persona 的 error 文案,不向上抛。"""
    from app.chat import render as render_mod

    class _FakeAgent:
        def __init__(self, cfg, tools=None):
            pass

        async def stream(self, messages, *, context, prompt_vars):
            yield StreamChunk(text="开头")
            raise RuntimeError("boom")

    monkeypatch.setattr(render_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(render_mod, "schedule_post_actions", _noop_post)

    out = []
    async for text in render_mod.render_chat_turn(
        _ctx(), outbound_message_id="m", session_id=None, channel="lark", features={},
    ):
        out.append(text)

    assert out[0] == "开头"
    assert out[-1] == "ERR"


# ---------------------------------------------------------------------------
# on_error="raise" 模式（codex 必改 2）：proactive 用。真人回复路径保持默认
# on_error="yield_text"（吞异常 / yield 错误文案给用户看）；proactive 路径必须能
# **拿到失败信号**而不是把"ERR / 遇到了问题 / 截断"当成功内容主动发给真人。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_raise_mode_stream_exception_raises_render_failed(monkeypatch):
    """on_error='raise'：stream 抛异常 → render 抛 RenderFailed（不 yield 错误文案）。

    承重（必改 2）：默认模式吞异常 yield 'ERR' 给用户看是对的（真人在等回复）；但
    proactive 复用它会把 'ERR' 当成功内容主动发给真人。raise 模式让调用方拿到失败信号。
    """
    from app.chat import render as render_mod
    from app.chat.render import RenderFailed

    class _FakeAgent:
        def __init__(self, cfg, tools=None):
            pass

        async def stream(self, messages, *, context, prompt_vars):
            yield StreamChunk(text="开头")
            raise RuntimeError("boom")

    monkeypatch.setattr(render_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(render_mod, "schedule_post_actions", _noop_post)

    out = []
    with pytest.raises(RenderFailed):
        async for text in render_mod.render_chat_turn(
            _ctx(), outbound_message_id="m", session_id=None, channel="lark",
            features={}, on_error="raise",
        ):
            out.append(text)

    # 抛错前已 yield 的正常片段允许在 out 里，但绝不能出现 persona error 文案。
    assert "ERR" not in out, "raise 模式绝不 yield persona error 文案"


@pytest.mark.asyncio
async def test_render_raise_mode_content_filter_raises_render_failed(monkeypatch):
    """on_error='raise'：content_filter → 抛 RenderFailed（不 yield persona CF 文案）。"""
    from app.chat import render as render_mod
    from app.chat.render import RenderFailed

    class _FakeAgent:
        def __init__(self, cfg, tools=None):
            pass

        async def stream(self, messages, *, context, prompt_vars):
            yield StreamChunk(finish_reason="content_filter")

    monkeypatch.setattr(render_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(render_mod, "schedule_post_actions", _noop_post)

    out = []
    with pytest.raises(RenderFailed):
        async for text in render_mod.render_chat_turn(
            _ctx(), outbound_message_id="m", session_id=None, channel="lark",
            features={}, on_error="raise",
        ):
            out.append(text)

    assert "CF" not in out, "raise 模式绝不 yield persona content_filter 文案"


@pytest.mark.asyncio
async def test_render_raise_mode_length_raises_render_failed(monkeypatch):
    """on_error='raise'：length 截断 → 抛 RenderFailed（不 yield 截断提示）。

    截断提示文案送给真人是出戏的半截消息——proactive 把它当失败、不出站。
    """
    from app.chat import render as render_mod
    from app.chat.render import RenderFailed

    class _FakeAgent:
        def __init__(self, cfg, tools=None):
            pass

        async def stream(self, messages, *, context, prompt_vars):
            yield StreamChunk(text="部分")
            yield StreamChunk(finish_reason="length")

    monkeypatch.setattr(render_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(render_mod, "schedule_post_actions", _noop_post)

    out = []
    with pytest.raises(RenderFailed):
        async for text in render_mod.render_chat_turn(
            _ctx(), outbound_message_id="m", session_id=None, channel="lark",
            features={}, on_error="raise",
        ):
            out.append(text)

    assert not any("截断" in t for t in out), "raise 模式绝不 yield 截断提示"


@pytest.mark.asyncio
async def test_render_raise_mode_clean_completion_still_yields_and_posts(monkeypatch):
    """on_error='raise' 不改正常路径：渲染成功照常 yield 文本 + 跑 post-actions。

    raise 模式只在**失败**时改语义（抛而非 yield 错误文案）；成功时与默认模式完全一致。
    """
    from app.chat import render as render_mod

    class _FakeAgent:
        def __init__(self, cfg, tools=None):
            pass

        async def stream(self, messages, *, context, prompt_vars):
            yield StreamChunk(text="你好")
            yield StreamChunk(text="呀")

    monkeypatch.setattr(render_mod, "Agent", _FakeAgent)
    posted = {}

    async def fake_post(**kwargs):
        posted.update(kwargs)

    monkeypatch.setattr(render_mod, "schedule_post_actions", fake_post)

    out = []
    async for text in render_mod.render_chat_turn(
        _ctx(), outbound_message_id="m", session_id=None, channel="lark",
        features={}, on_error="raise",
    ):
        out.append(text)

    assert "".join(out) == "你好呀"
    assert posted["full_content"] == "你好呀", "成功路径仍跑 post-actions"


async def _noop_post(**kwargs):
    return None
