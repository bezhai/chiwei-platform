"""T1 — neutral types round-trip coverage.

These tests pin the *shape* of the provider-agnostic types that the thinking
core exchanges with every adapter. They deliberately do NOT touch langchain:
the neutral layer is what replaces it. The hard requirement (spec §Key design
decisions) is that these types carry, without loss:

  - multimodal content blocks (chat-history images + OpenAI-style image_url
    blocks returned by tools),
  - the sequence a model turn came back as (thoughts / text / calls, in order,
    each with its own signature),
  - string-content normalisation (deepseek rejects arrays / null content),
  - tool_call + tool_result.

No real provider wire layout is encoded here — adapters own that translation.
"""

from __future__ import annotations

from app.agent.neutral import (
    ContentBlock,
    Message,
    Role,
    StreamChunk,
    ToolCall,
    ToolDef,
    ToolResult,
    TurnPart,
    TurnPartKind,
    normalize_content_to_text,
)


# ---------------------------------------------------------------------------
# Message: roles + plain string content
# ---------------------------------------------------------------------------


def test_message_plain_string_content():
    msg = Message(role=Role.USER, content="hello")
    assert msg.role == Role.USER
    assert msg.content == "hello"
    assert msg.turn_parts == []
    assert msg.tool_calls == []


def test_message_roles_cover_chat_history_kinds():
    # system / user / assistant / tool — the four roles chat history + tool
    # loop need.
    assert Role.SYSTEM != Role.USER
    assert {Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.TOOL} == set(Role)


# ---------------------------------------------------------------------------
# Message: multimodal content blocks (chat-history image + tool image_url)
# ---------------------------------------------------------------------------


def test_message_carries_chat_history_image_block():
    # The shape build_group_messages / build_p2p_messages produce: a text
    # block followed by an image block referencing a URL.
    blocks = [
        ContentBlock.from_text("@photo.png:"),
        ContentBlock.from_image(url="https://cdn.example/photo.png"),
    ]
    msg = Message(role=Role.USER, content=blocks)
    assert isinstance(msg.content, list)
    assert msg.content[0].type == "text"
    assert msg.content[0].text == "@photo.png:"
    assert msg.content[1].type == "image"
    assert msg.content[1].url == "https://cdn.example/photo.png"


def test_content_block_to_from_dict_round_trip():
    # text / image / image_url all survive a dict round-trip — the guardrail
    # T2/T4 adapter authors rely on when serialising history.
    for block in (
        ContentBlock.from_text("hi"),
        ContentBlock.from_image(url="https://cdn.example/p.png"),
        ContentBlock.from_image_url({"url": "https://cdn.example/x.png"}),
    ):
        assert ContentBlock.from_dict(block.to_dict()) == block


def test_message_carries_openai_style_tool_image_url_block():
    # Tools can return OpenAI-style image_url blocks; the neutral block must
    # round-trip that without the adapter having spoken yet.
    block = ContentBlock.from_image_url(
        {"url": "https://cdn.example/x.png", "detail": "high"}
    )
    msg = Message(role=Role.TOOL, content=[block], tool_call_id="call_1")
    assert msg.content[0].type == "image_url"
    assert msg.content[0].image_url == {
        "url": "https://cdn.example/x.png",
        "detail": "high",
    }
    assert msg.tool_call_id == "call_1"


# ---------------------------------------------------------------------------
# string-content normalisation (deepseek rejects arrays / null)
# ---------------------------------------------------------------------------


def test_normalize_content_flattens_blocks_to_text():
    blocks = [
        ContentBlock.from_text("hello "),
        ContentBlock.from_image(url="https://cdn.example/p.png"),
        ContentBlock.from_text("world"),
    ]
    assert normalize_content_to_text(blocks) == "hello world"


def test_normalize_content_none_becomes_empty_string():
    assert normalize_content_to_text(None) == ""


def test_normalize_content_passes_through_plain_string():
    assert normalize_content_to_text("already text") == "already text"


def test_message_text_helper_normalises_blocks():
    msg = Message(
        role=Role.ASSISTANT,
        content=[ContentBlock.from_text("a"), ContentBlock.from_text("b")],
    )
    assert msg.text() == "ab"


# ---------------------------------------------------------------------------
# tool_call round-trip on an assistant message
# ---------------------------------------------------------------------------


def test_assistant_message_carries_tool_calls():
    tc = ToolCall(id="call_42", name="search_web", arguments={"query": "cats"})
    msg = Message(role=Role.ASSISTANT, content="", tool_calls=[tc])
    assert msg.tool_calls[0].id == "call_42"
    assert msg.tool_calls[0].name == "search_web"
    assert msg.tool_calls[0].arguments == {"query": "cats"}

    dumped = msg.to_dict()
    back = Message.from_dict(dumped)
    assert back.tool_calls[0].name == "search_web"
    assert back.tool_calls[0].arguments == {"query": "cats"}


def test_tool_call_carries_opaque_signature_but_keeps_to_dict_json_safe():
    """ToolCall holds an opaque provider signature (Gemini thought_signature) in
    memory, but to_dict() must stay JSON-serialisable for langfuse spans, so the
    raw bytes are not emitted."""
    import json

    tc = ToolCall(
        id="c1", name="load_skill", arguments={"name": "x"}, signature=b"sig-abc"
    )
    assert tc.signature == b"sig-abc"

    dumped = tc.to_dict()
    # bytes must not leak into the trace payload (would break JSON serialisation)
    json.dumps(dumped)  # raises if a non-serialisable value snuck in
    assert "signature" not in dumped


def test_tool_result_round_trip():
    res = ToolResult(tool_call_id="call_42", content="found 3 cats")
    assert res.tool_call_id == "call_42"
    assert res.content == "found 3 cats"
    # tool result becomes a tool-role message in the loop
    msg = res.to_message()
    assert msg.role == Role.TOOL
    assert msg.tool_call_id == "call_42"
    assert msg.text() == "found 3 cats"


# ---------------------------------------------------------------------------
# ToolDef
# ---------------------------------------------------------------------------


def test_tool_def_holds_name_description_and_json_schema():
    td = ToolDef(
        name="search_web",
        description="search the web",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    assert td.name == "search_web"
    assert td.parameters["properties"]["query"]["type"] == "string"


# ---------------------------------------------------------------------------
# StreamChunk — must serve app/chat/stream.py's consumption contract
# ---------------------------------------------------------------------------


def test_stream_chunk_text_token():
    chunk = StreamChunk(text="hi")
    assert chunk.text == "hi"
    assert chunk.finish_reason is None
    assert chunk.tool_call is None
    assert chunk.reasoning is None


def test_stream_chunk_finish_reason_content_filter():
    chunk = StreamChunk(finish_reason="content_filter")
    assert chunk.finish_reason == "content_filter"


def test_stream_chunk_finish_reason_length():
    chunk = StreamChunk(finish_reason="length")
    assert chunk.finish_reason == "length"


def test_stream_chunk_tool_call_boundary():
    # consumer detects the text->tool boundary by a chunk carrying a tool_call
    chunk = StreamChunk(
        tool_call=ToolCall(id="c1", name="search_web", arguments={})
    )
    assert chunk.tool_call is not None
    assert chunk.tool_call.name == "search_web"


def test_stream_chunk_tool_result():
    chunk = StreamChunk(
        tool_result=ToolResult(tool_call_id="c1", content="done")
    )
    assert chunk.tool_result is not None
    assert chunk.tool_result.content == "done"


def test_stream_chunk_reasoning_passthrough():
    chunk = StreamChunk(reasoning="thinking...")
    assert chunk.reasoning == "thinking..."


# ---------------------------------------------------------------------------
# Lossless replay serialization (session 续接)
# ---------------------------------------------------------------------------
#
# ``to_dict`` / ``from_dict`` deliberately drop ``ToolCall.signature`` (the
# gemini ``thought_signature`` blob) because they serve langfuse tracing, not
# the wire. Replaying a stored transcript back into the model is a different
# job: it MUST be lossless, including provider-private blobs, or the model's
# behaviour drifts on the next turn. ``to_replay_dict`` / ``from_replay_dict``
# are that lossless path — they round-trip through JSON (no raw bytes).


def test_tool_call_replay_roundtrip_preserves_signature():
    import json

    tc = ToolCall(
        id="c1",
        name="emit_event",
        arguments={"summary": "晚餐"},
        signature=b"\x00\xff\x10gemini-thought",
    )
    # round-trips through JSON (Redis stores text) without loss
    blob = json.dumps(tc.to_replay_dict())
    restored = ToolCall.from_replay_dict(json.loads(blob))
    assert restored.id == "c1"
    assert restored.name == "emit_event"
    assert restored.arguments == {"summary": "晚餐"}
    assert restored.signature == b"\x00\xff\x10gemini-thought"


def test_tool_call_replay_roundtrip_without_signature():
    tc = ToolCall(id="c2", name="sleep", arguments={})
    restored = ToolCall.from_replay_dict(tc.to_replay_dict())
    assert restored.signature is None


def test_to_dict_still_drops_signature_for_tracing():
    # the existing tracing path must be unchanged: it omits signature.
    tc = ToolCall(id="c1", name="x", arguments={}, signature=b"abc")
    assert "signature" not in tc.to_dict()


def test_message_replay_roundtrip_preserves_tool_calls_with_signature():
    import json

    msg = Message(
        role=Role.ASSISTANT,
        content="thinking out loud",
        tool_calls=[
            ToolCall(
                id="c1",
                name="emit_event",
                arguments={"summary": "晚餐进行中"},
                signature=b"\x01\x02sig",
            )
        ],
    )
    blob = json.dumps(msg.to_replay_dict())
    restored = Message.from_replay_dict(json.loads(blob))
    assert restored.role == Role.ASSISTANT
    assert restored.content == "thinking out loud"
    assert len(restored.tool_calls) == 1
    assert restored.tool_calls[0].signature == b"\x01\x02sig"
    assert restored.tool_calls[0].arguments == {"summary": "晚餐进行中"}


def test_message_replay_roundtrip_preserves_multimodal_content():
    import json

    msg = Message(
        role=Role.TOOL,
        content=[
            ContentBlock.from_text("@3.png:"),
            ContentBlock.from_image_url({"url": "https://x/3.png"}),
        ],
        tool_call_id="c1",
    )
    restored = Message.from_replay_dict(json.loads(json.dumps(msg.to_replay_dict())))
    assert isinstance(restored.content, list)
    assert restored.content[0].type == "text"
    assert restored.content[1].type == "image_url"
    assert restored.content[1].image_url == {"url": "https://x/3.png"}
    assert restored.tool_call_id == "c1"


# ---------------------------------------------------------------------------
# A model turn is a *sequence*, not three piles
# ---------------------------------------------------------------------------
#
# The model can come back with "thought, text, call, another thought, another
# call", and each segment carries its own opaque signature. Splitting that into
# a joined text + a list of calls loses both the order and which signature
# belonged to which segment, and the provider demands the whole turn back
# verbatim on the next request. ``Message.turn_parts`` is that sequence.


def test_a_model_turn_keeps_the_order_it_came_back_in():
    calls = [
        ToolCall(id="c1", name="look_around", arguments={}),
        ToolCall(id="c2", name="say", arguments={"words": "嗯"}),
    ]
    parts = [
        TurnPart.from_thought("先看看四周", signature=b"sig-1"),
        TurnPart.from_text("好"),
        TurnPart.from_tool_call(calls[0]),
        TurnPart.from_thought("再说一句", signature=b"sig-2"),
        TurnPart.from_tool_call(calls[1]),
    ]

    msg = Message.from_model_turn(parts, calls)

    assert [p.kind for p in msg.turn_parts] == [
        TurnPartKind.THOUGHT,
        TurnPartKind.TEXT,
        TurnPartKind.TOOL_CALL,
        TurnPartKind.THOUGHT,
        TurnPartKind.TOOL_CALL,
    ]
    assert [p.call_id for p in msg.turn_parts if p.call_id] == ["c1", "c2"]
    # the plain-text view everyone else reads is still just the spoken text
    assert msg.content == "好"
    assert msg.text() == "好"
    assert [tc.id for tc in msg.tool_calls] == ["c1", "c2"]


def test_each_thought_keeps_its_own_signature():
    parts = [
        TurnPart.from_thought("第一段", signature=b"\x00sig-a"),
        TurnPart.from_thought("第二段", signature=b"\xffsig-b"),
    ]
    msg = Message.from_model_turn(parts, [])

    assert [p.signature for p in msg.turn_parts] == [b"\x00sig-a", b"\xffsig-b"]
    assert msg.thought_text() == "第一段第二段"


def test_a_text_part_can_carry_a_signature_too():
    parts = [TurnPart.from_text("好", signature=b"sig-on-text")]
    msg = Message.from_model_turn(parts, [])

    assert msg.turn_parts[0].signature == b"sig-on-text"
    assert msg.content == "好"


def test_a_message_nobody_built_from_a_model_turn_has_no_sequence():
    msg = Message(role=Role.USER, content="hello")
    assert msg.turn_parts == []
    assert msg.thought_text() == ""


def test_the_model_turn_survives_a_replay_roundtrip_byte_for_byte():
    import json

    calls = [ToolCall(id="c1", name="say", arguments={"words": "嗯"}, signature=b"s1")]
    parts = [
        TurnPart.from_thought("想了想", signature=b"\x00\xffthought-sig"),
        TurnPart.from_text("嗯"),
        TurnPart.from_tool_call(calls[0]),
    ]
    msg = Message.from_model_turn(parts, calls)

    blob = json.dumps(msg.to_replay_dict())
    restored = Message.from_replay_dict(json.loads(blob))

    assert [p.kind for p in restored.turn_parts] == [
        TurnPartKind.THOUGHT,
        TurnPartKind.TEXT,
        TurnPartKind.TOOL_CALL,
    ]
    assert restored.turn_parts[0].text == "想了想"
    assert restored.turn_parts[0].signature == b"\x00\xffthought-sig"
    assert restored.turn_parts[1].text == "嗯"
    assert restored.turn_parts[2].call_id == "c1"
    assert restored.tool_calls[0].signature == b"s1"
    assert restored.content == "嗯"


def test_a_signature_with_no_text_still_survives_the_replay_roundtrip():
    import json

    msg = Message.from_model_turn([TurnPart.from_text("", signature=b"bare")], [])
    restored = Message.from_replay_dict(json.loads(json.dumps(msg.to_replay_dict())))

    assert restored.turn_parts[0].signature == b"bare"
    assert restored.turn_parts[0].text == ""


def test_the_langfuse_dict_shows_the_thought_and_the_signature_size_only():
    msg = Message.from_model_turn(
        [
            TurnPart.from_thought("想了想", signature=b"\x00\xffsig"),
            TurnPart.from_text("嗯"),
        ],
        [],
    )
    dumped = msg.to_dict()

    assert dumped["turn_parts"] == [
        {"kind": "thought", "text": "想了想", "signature_bytes": 5},
        {"kind": "text", "text": "嗯"},
    ]
    assert Message.from_dict(dumped).turn_parts[0].text == "想了想"
    assert Message.from_dict(dumped).turn_parts[0].signature is None


def test_stream_chunk_carries_the_signature_riding_on_its_part():
    chunk = StreamChunk(reasoning="想了想", signature=b"sig-1")
    assert chunk.signature == b"sig-1"
    assert StreamChunk(text="hi").signature is None
