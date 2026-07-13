"""chat_node 单元测试（Task 7-11 累积）。

剥离 + 渲染层抽取后:chat_node 不再调单一 ``_build_and_stream``,而是
``build_human_chat_context``(真人 context 构建)→ ``render_chat_turn``(共享渲染层)。
这些测试锁的是 chat_node 的 segmentation / pre-safety / 出站逻辑,所以把 context
构建打桩成返回一个 stub ChatTurnContext、把渲染层打桩成喂入预设的 token 流。
"""
import asyncio

import pytest

from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment


def _chat_segments(emitted):
    return [item for item in emitted if isinstance(item, ChatResponseSegment)]


def _stub_turn_ctx():
    """chat_node segmentation 测试不关心 context 内容,给个最小 stub。"""
    from app.chat.render import ChatTurnContext

    return ChatTurnContext(
        messages=[], image_registry=None, chat_id="c1", persona_id="p1",
        identity="", appearance="", inner_context="", persona=None,
    )


def _patch_context(monkeypatch, cn):
    """把真人 context 构建打桩成返回 stub(非 None,让 chat_node 走渲染分支)。"""
    async def fake_build_ctx(message_id, *, persona_id, **k):
        return _stub_turn_ctx()

    monkeypatch.setattr(cn, "build_human_chat_context", fake_build_ctx, raising=False)


@pytest.fixture
def base_request():
    return ChatRequest(
        message_id="m1", persona_id="p1", session_id="s1",
        channel="qq", chat_id="c1", is_p2p=True, user_id="u1", lane="dev",
    )


@pytest.mark.asyncio
async def test_chat_node_prep_block_calls_dependencies(monkeypatch, base_request):
    """prep 块按顺序调用 find_message_content / parse_content / find_gray_config /
    fetch_guard_message / run_pre_safety_check。
    """
    from app.nodes import chat_node as cn

    calls = []

    async def fake_find_message(mid):
        calls.append(("find_message_content", mid))
        return "hello world"

    async def fake_find_gray(mid):
        calls.append(("find_gray_config", mid))
        return {"gray": "x"}

    async def fake_guard(persona):
        calls.append(("fetch_guard_message", persona))
        return "guard say no"

    async def fake_pre_safety(message_id, content, persona_id):
        calls.append(("run_pre_safety_check", message_id, persona_id))
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(
            pre_request_id="x", message_id=message_id, is_blocked=False,
        )

    async def fake_render(*a, **k):
        calls.append(("render_channel", k.get("channel")))
        if False:
            yield ""

    async def fake_resolve_bot(pid, cid):
        return "resolved-bot"

    async def fake_set_bot(sid, bn, pid):
        pass

    async def fake_emit(d):
        pass

    def parse_content_fn(s):
        calls.append(("parse_content", s))

        class _P:
            def render(self):
                return s

        return _P()

    monkeypatch.setattr(cn, "find_message_content", fake_find_message)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre_safety)
    monkeypatch.setattr(cn, "parse_content", parse_content_fn)
    _patch_context(monkeypatch, cn)
    monkeypatch.setattr(
        cn, "render_chat_turn", fake_render, raising=False
    )
    monkeypatch.setattr(
        cn, "resolve_bot_name_for_persona", fake_resolve_bot, raising=False
    )
    monkeypatch.setattr(
        cn, "set_agent_response_bot", fake_set_bot, raising=False
    )
    monkeypatch.setattr(cn, "emit", fake_emit, raising=False)

    await cn.chat_node(base_request)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    names = [c[0] for c in calls]
    assert "find_message_content" in names
    assert "parse_content" in names
    assert "find_gray_config" in names
    assert "fetch_guard_message" in names
    assert "run_pre_safety_check" in names
    assert ("render_channel", "qq") in calls
    assert names.index("find_message_content") < names.index("parse_content")
    assert names.index("parse_content") < names.index("run_pre_safety_check")


@pytest.mark.asyncio
async def test_chat_node_emits_not_found_when_no_message(monkeypatch, base_request):
    from app.domain.chat_dataflow import ChatResponseSegment
    from app.nodes import chat_node as cn

    async def fake_find_message(mid): return None
    async def fake_find_gray(mid): return {}
    async def fake_guard(persona): return "guard"
    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)

    monkeypatch.setattr(cn, "find_message_content", fake_find_message)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)

    emitted: list[ChatResponseSegment] = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert len(emitted) == 1
    seg = emitted[0]
    assert "未找到相关消息记录" in seg.content
    assert seg.is_last is True
    assert seg.message_id == "m1"
    assert seg.persona_id == "p1"
    assert seg.lane == "dev"


@pytest.mark.asyncio
async def test_chat_node_resolves_bot_name_and_updates_agent_response(monkeypatch, base_request):
    from app.nodes import chat_node as cn

    async def fake_find_message(mid): return "hi"
    async def fake_find_gray(mid): return {}
    async def fake_guard(persona): return "guard"
    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)
    async def fake_render(*a, **k):
        if False:
            yield ""

    resolved_calls = []
    set_calls = []
    async def fake_resolve(persona_id, chat_id):
        resolved_calls.append((persona_id, chat_id))
        return "resolved-bot-x"
    async def fake_set(session_id, bot_name, persona_id):
        set_calls.append((session_id, bot_name, persona_id))

    monkeypatch.setattr(cn, "find_message_content", fake_find_message)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    _patch_context(monkeypatch, cn)
    monkeypatch.setattr(cn, "render_chat_turn", fake_render, raising=False)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert resolved_calls == [("p1", "c1")]
    assert set_calls == [("s1", "resolved-bot-x", "p1")]


@pytest.mark.asyncio
async def test_chat_node_passes_req_bot_name_to_build_context(monkeypatch):
    """chat_node 用 req.bot_name（接收消息的 bot）调 build_human_chat_context，
    让入站图下载拿到正确 X-App-Name。修复前不传 → build_ctx 收到默认空 →
    process_image 422、用户图丢失（trace dbde982e146840cc00610c393fc5820e）。"""
    from app.nodes import chat_node as cn

    req = ChatRequest(
        message_id="m1", persona_id="p1", session_id="s1", channel="qq",
        chat_id="c1", is_p2p=True, user_id="u1", lane="dev", bot_name="bot-recv",
    )

    captured: dict[str, object] = {}

    async def fake_build_ctx(message_id, *, persona_id, bot_name="", **k):
        captured["bot_name"] = bot_name
        return _stub_turn_ctx()

    async def fake_find_msg(mid): return "hi"
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "guard"
    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)
    async def fake_render(*a, **k):
        if False:
            yield ""
    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(sid, bn, pid): pass
    async def fake_emit(d): pass

    monkeypatch.setattr(cn, "build_human_chat_context", fake_build_ctx, raising=False)
    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "render_chat_turn", fake_render, raising=False)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(req)

    assert captured.get("bot_name") == "bot-recv", (
        f"chat_node 必须用 req.bot_name 调 build_human_chat_context，"
        f"实得 {captured.get('bot_name')!r}"
    )


SPLIT = "---split---"


@pytest.mark.asyncio
async def test_chat_node_split_two_segments_then_final(monkeypatch, base_request):
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)

    async def fake_stream(*a, **k):
        for piece in ["hello ", SPLIT, " world", SPLIT, " foo"]:
            yield piece

    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(sid, bn, pid): pass
    async def fake_find_msg(mid): return "input"
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "guard"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    _patch_context(monkeypatch, cn)
    monkeypatch.setattr(cn, "render_chat_turn", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    segments = _chat_segments(emitted)
    assert len(segments) == 3
    assert segments[0].part_index == 0
    assert segments[0].content == "hello"
    assert segments[0].is_last is False
    assert segments[1].part_index == 1
    assert segments[1].content == "world"
    assert segments[1].is_last is False
    assert segments[2].part_index == 2
    assert segments[2].is_last is True
    assert "foo" in segments[2].content
    assert segments[2].full_content is not None
    assert SPLIT not in segments[2].full_content
    for s in segments:
        assert s.lane == "dev"
        assert s.bot_name == "bot-x"


@pytest.mark.asyncio
async def test_chat_node_pre_safety_block_at_first_boundary(monkeypatch, base_request):
    """verdict=BLOCK 在第一个段边界返回 -> emit 1 段 guard + is_last=True，无后续。"""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=True)

    async def fake_stream(*a, **k):
        for p in ["hello", SPLIT, " world", SPLIT, " final"]:
            yield p

    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(*a, **k): pass
    async def fake_find_msg(mid): return "input"
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "GUARD_TEXT"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    _patch_context(monkeypatch, cn)
    monkeypatch.setattr(cn, "render_chat_turn", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert len(emitted) == 1
    assert emitted[0].content == "GUARD_TEXT"
    assert emitted[0].is_last is True
    assert emitted[0].full_content == "GUARD_TEXT"


@pytest.mark.asyncio
async def test_chat_node_pre_safety_block_at_final(monkeypatch, base_request):
    """stream 已结束（无 SPLIT），verdict 在 final 段到达时为 BLOCK。"""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=True)

    async def fake_stream(*a, **k):
        for p in ["just one piece"]:
            yield p

    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(*a, **k): pass
    async def fake_find_msg(mid): return "input"
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "GUARD_TEXT"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    _patch_context(monkeypatch, cn)
    monkeypatch.setattr(cn, "render_chat_turn", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert len(emitted) == 1
    assert emitted[0].content == "GUARD_TEXT"
    assert emitted[0].is_last is True


@pytest.mark.asyncio
async def test_chat_node_caps_mid_segments_at_max_messages_minus_one(monkeypatch, base_request):
    """Stream with 5+ SPLITs should produce at most MAX_MESSAGES-1=3 mid + 1 final."""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)

    async def fake_stream(*a, **k):
        for p in ["p0", SPLIT, " p1", SPLIT, " p2", SPLIT, " p3", SPLIT, " p4"]:
            yield p

    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(*a, **k): pass
    async def fake_find_msg(mid): return "input"
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "guard"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    _patch_context(monkeypatch, cn)
    monkeypatch.setattr(cn, "render_chat_turn", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    segments = _chat_segments(emitted)
    assert len(segments) == 4
    assert segments[0].is_last is False
    assert segments[1].is_last is False
    assert segments[2].is_last is False
    assert segments[3].is_last is True
    assert "p3" in segments[3].full_content
    assert "p4" in segments[3].full_content


@pytest.mark.asyncio
async def test_chat_node_no_split_emits_single_final_segment(monkeypatch, base_request):
    """Stream with no SPLIT_MARKER → 1 final segment, content = full_content."""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)

    async def fake_stream(*a, **k):
        for p in ["only ", "one ", "piece"]:
            yield p

    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(*a, **k): pass
    async def fake_find_msg(mid): return "input"
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "guard"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    _patch_context(monkeypatch, cn)
    monkeypatch.setattr(cn, "render_chat_turn", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    segments = _chat_segments(emitted)
    assert len(segments) == 1
    assert segments[0].part_index == 0
    assert segments[0].is_last is True
    assert "only one piece" in segments[0].content
    assert segments[0].full_content == "only one piece"


@pytest.mark.asyncio
async def test_chat_node_skips_fully_empty_final_segment(monkeypatch, base_request):
    """LLM 单轮返回空文本+空 tool_calls 时(trace 82323210372fe067ec2a60abd8e9fdb3
    的收尾轮场景),render_chat_turn 的 stream 从头到尾不产出任何 token。
    channel-server 侧(chat-response-handler.ts)本来就有"content 为空 +
    is_last=True → 不发送给用户但标记 completed"的分支,所以 chat_node 仍要
    emit 这条收尾消息(不能整段跳过,否则 common_agent_response.status 永远卡
    在 pending、拿不到完成标记),只是 content 必须是真正的空字符串,不能带任何
    空气泡文案。"""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)

    async def fake_stream(*a, **k):
        return
        yield  # pragma: no cover - 让函数保持 async generator 形态，但从不 yield

    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(*a, **k): pass
    async def fake_find_msg(mid): return "input"
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "guard"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    _patch_context(monkeypatch, cn)
    monkeypatch.setattr(cn, "render_chat_turn", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    segments = _chat_segments(emitted)
    assert len(segments) == 1, "仍要 emit 一条收尾消息,让 channel-server 能标记完成"
    assert segments[0].content == "", "content 必须是真正的空字符串,不能带空气泡文案"
    assert segments[0].is_last is True
    assert segments[0].status == "success"


@pytest.mark.asyncio
async def test_chat_node_skips_whitespace_only_final_segment(monkeypatch, base_request):
    """LLM 只吐出空白字符(非严格空串)时,emit 出去的 content 必须被 strip 干净。

    复现原 final_content 兜底分支的坑：``remaining`` strip 后为空、
    ``part_index == 0`` 时会退回**未 strip** 的 ``full_content``——若
    ``full_content`` 本身只是空白（如模型吐了几个空格 / 换行 token），未 strip
    的空白字符串在 Python 侧是 truthy。channel-server 侧用 JS 的 ``!content``
    判断是否要发送,非空白字符串(比如一个空格)在 JS 里同样是 truthy,会绕过
    channel-server 已有的空内容分支被当"非空"内容真的发出去——这正是用户偶发
    看到"空气泡"的根因。这里必须确保 emit 出去的 content 是 strip 后的空字符
    串,而不是原始空白。"""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)

    async def fake_stream(*a, **k):
        for p in ["  ", "\n", " "]:
            yield p

    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(*a, **k): pass
    async def fake_find_msg(mid): return "input"
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "guard"

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    _patch_context(monkeypatch, cn)
    monkeypatch.setattr(cn, "render_chat_turn", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    segments = _chat_segments(emitted)
    assert len(segments) == 1
    assert segments[0].content == "", "空白字符不能绕过 strip 被当成非空内容发出去"
    assert segments[0].is_last is True
    assert segments[0].status == "success"


@pytest.mark.asyncio
async def test_resolve_pre_safety_for_part_fail_open_on_timeout():
    """Helper should return ALLOW with original content when pre_task times out."""
    from app.nodes.chat_node import _resolve_pre_safety_for_part

    async def slow_pre():
        await asyncio.sleep(10)
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=True)

    pre_task = asyncio.create_task(slow_pre())
    try:
        result = await _resolve_pre_safety_for_part(
            "hello", pre_task, "GUARD", timeout=0.01,
        )
        assert result.blocked is False
        assert result.content == "hello"
    finally:
        pre_task.cancel()


@pytest.mark.asyncio
async def test_resolve_pre_safety_for_part_fail_open_on_exception():
    """Helper should return ALLOW with original content when pre_task raises."""
    from app.nodes.chat_node import _resolve_pre_safety_for_part

    async def failing_pre():
        raise ValueError("simulated failure")

    pre_task = asyncio.create_task(failing_pre())
    try:
        await pre_task
    except ValueError:
        pass

    result = await _resolve_pre_safety_for_part("hello", pre_task, "GUARD")
    assert result.blocked is False
    assert result.content == "hello"


@pytest.mark.asyncio
async def test_chat_node_propagates_channel_to_response_segment(monkeypatch):
    """chat_node 产出的 ChatResponseSegment 必须透传 req.channel。
    走 fetch-empty 分支（raw_content 空 -> emit 一段后 return），覆盖
    显式字段那处 emit；base_payload 路径由 route_chat_node TDD 同构保护。
    """
    from app.nodes import chat_node as cn

    async def fake_find_message(mid):
        return ""  # raw_content 空 -> 走 fetch-empty 分支

    async def fake_find_gray(mid):
        return {}

    async def fake_guard(persona):
        return "guard"

    async def fake_pre_safety(message_id, content, persona_id):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(
            pre_request_id="x", message_id=message_id, is_blocked=False,
        )

    def parse_content_fn(s):
        class _P:
            def render(self):
                return s

        return _P()

    emitted = []

    async def fake_emit(d):
        emitted.append(d)

    monkeypatch.setattr(cn, "find_message_content", fake_find_message)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre_safety)
    monkeypatch.setattr(cn, "parse_content", parse_content_fn)
    monkeypatch.setattr(cn, "emit", fake_emit)

    req = ChatRequest(
        message_id="m1", persona_id="p1", session_id="s1", channel="qq",
    )
    await cn.chat_node(req)

    assert len(emitted) >= 1
    assert all(seg.channel == "qq" for seg in emitted)
