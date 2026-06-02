"""T1 — neutral types round-trip coverage.

These tests pin the *shape* of the provider-agnostic types that the thinking
core exchanges with every adapter. They deliberately do NOT touch langchain:
the neutral layer is what replaces it. The hard requirement (spec §Key design
decisions) is that these types carry, without loss:

  - multimodal content blocks (chat-history images + OpenAI-style image_url
    blocks returned by tools),
  - deepseek ``reasoning_content`` passthrough on assistant messages,
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
    normalize_content_to_text,
)


# ---------------------------------------------------------------------------
# Message: roles + plain string content
# ---------------------------------------------------------------------------


def test_message_plain_string_content():
    msg = Message(role=Role.USER, content="hello")
    assert msg.role == Role.USER
    assert msg.content == "hello"
    assert msg.reasoning_content is None
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
# Message: deepseek reasoning_content passthrough
# ---------------------------------------------------------------------------


def test_assistant_message_carries_reasoning_content():
    msg = Message(
        role=Role.ASSISTANT,
        content="final answer",
        reasoning_content="let me think step by step ...",
    )
    assert msg.reasoning_content == "let me think step by step ..."
    # passthrough survives a serialize / reconstruct round-trip
    dumped = msg.to_dict()
    assert dumped["reasoning_content"] == "let me think step by step ..."
    back = Message.from_dict(dumped)
    assert back.reasoning_content == "let me think step by step ..."
    assert back.content == "final answer"


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
