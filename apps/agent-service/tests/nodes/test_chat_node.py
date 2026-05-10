"""chat_node 单元测试（Task 7-11 累积）。"""
import asyncio

import pytest

from app.domain.chat_dataflow import ChatRequest


@pytest.fixture
def base_request():
    return ChatRequest(
        message_id="m1", persona_id="p1", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="dev",
    )


@pytest.mark.asyncio
async def test_chat_node_prep_block_calls_dependencies(monkeypatch, base_request):
    """prep 块按顺序调用 find_message_content / parse_content / find_gray_config /
    fetch_guard_message / run_pre_safety_via_graph。
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
        calls.append(("run_pre_safety_via_graph", message_id, persona_id))
        from app.chat.pre_safety_gate import PreSafetyVerdict
        return PreSafetyVerdict(
            pre_request_id="x", message_id=message_id, is_blocked=False,
        )

    async def fake_build_and_stream(*a, **k):
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
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre_safety)
    monkeypatch.setattr(cn, "parse_content", parse_content_fn)
    monkeypatch.setattr(
        cn, "_build_and_stream", fake_build_and_stream, raising=False
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
    assert "run_pre_safety_via_graph" in names
    assert names.index("find_message_content") < names.index("parse_content")
    assert names.index("parse_content") < names.index("run_pre_safety_via_graph")


@pytest.mark.asyncio
async def test_chat_node_emits_not_found_when_no_message(monkeypatch, base_request):
    from app.domain.chat_dataflow import ChatResponseSegment
    from app.nodes import chat_node as cn

    async def fake_find_message(mid): return None
    async def fake_find_gray(mid): return {}
    async def fake_guard(persona): return "guard"
    async def fake_pre(*a, **k):
        from app.chat.pre_safety_gate import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)

    monkeypatch.setattr(cn, "find_message_content", fake_find_message)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)

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
        from app.chat.pre_safety_gate import PreSafetyVerdict
        return PreSafetyVerdict(pre_request_id="x", message_id="m1", is_blocked=False)
    async def fake_build_and_stream(*a, **k):
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
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_build_and_stream, raising=False)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert resolved_calls == [("p1", "c1")]
    assert set_calls == [("s1", "resolved-bot-x", "p1")]


SPLIT = "---split---"


@pytest.mark.asyncio
async def test_chat_node_split_two_segments_then_final(monkeypatch, base_request):
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.chat.pre_safety_gate import PreSafetyVerdict
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
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert len(emitted) == 3
    assert emitted[0].part_index == 0
    assert emitted[0].content == "hello"
    assert emitted[0].is_last is False
    assert emitted[1].part_index == 1
    assert emitted[1].content == "world"
    assert emitted[1].is_last is False
    assert emitted[2].part_index == 2
    assert emitted[2].is_last is True
    assert "foo" in emitted[2].content
    assert emitted[2].full_content is not None
    assert SPLIT not in emitted[2].full_content
    for s in emitted:
        assert s.lane == "dev"
        assert s.bot_name == "bot-x"


@pytest.mark.asyncio
async def test_chat_node_pre_safety_block_at_first_boundary(monkeypatch, base_request):
    """verdict=BLOCK 在第一个段边界返回 -> emit 1 段 guard + is_last=True，无后续。"""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.chat.pre_safety_gate import PreSafetyVerdict
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
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)

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
        from app.chat.pre_safety_gate import PreSafetyVerdict
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
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)

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
        from app.chat.pre_safety_gate import PreSafetyVerdict
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
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert len(emitted) == 4
    assert emitted[0].is_last is False
    assert emitted[1].is_last is False
    assert emitted[2].is_last is False
    assert emitted[3].is_last is True
    assert "p3" in emitted[3].full_content
    assert "p4" in emitted[3].full_content


@pytest.mark.asyncio
async def test_chat_node_no_split_emits_single_final_segment(monkeypatch, base_request):
    """Stream with no SPLIT_MARKER → 1 final segment, content = full_content."""
    from app.nodes import chat_node as cn

    async def fake_pre(*a, **k):
        from app.chat.pre_safety_gate import PreSafetyVerdict
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
    monkeypatch.setattr(cn, "run_pre_safety_via_graph", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)

    emitted = []
    async def fake_emit(d): emitted.append(d)
    monkeypatch.setattr(cn, "emit", fake_emit)

    await cn.chat_node(base_request)

    assert len(emitted) == 1
    assert emitted[0].part_index == 0
    assert emitted[0].is_last is True
    assert "only one piece" in emitted[0].content
    assert emitted[0].full_content == "only one piece"


@pytest.mark.asyncio
async def test_resolve_pre_safety_for_part_fail_open_on_timeout():
    """Helper should return ALLOW with original content when pre_task times out."""
    from app.nodes.chat_node import _resolve_pre_safety_for_part

    async def slow_pre():
        await asyncio.sleep(10)
        from app.chat.pre_safety_gate import PreSafetyVerdict
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
