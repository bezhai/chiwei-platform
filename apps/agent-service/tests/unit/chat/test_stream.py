"""Tests for app.chat.stream — StreamState + handle_token.

Covers:
  - Stream state tracking (token counting, content accumulation)
  - Content filter and length truncation signals
  - Tool call boundary split marker injection

Originally in test_pipeline.py; relocated when pipeline.py was deleted
in Phase 5a Task 12.
"""

from unittest.mock import MagicMock

from app.chat.stream import (
    SPLIT_MARKER,
    StreamState,
    handle_token,
    is_content_filter,
    is_length_truncated,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ai_chunk(
    text: str = "",
    finish_reason: str | None = None,
    tool_call_chunks: list | None = None,
):
    """Create a mock AIMessageChunk."""
    from langchain_core.messages import AIMessageChunk

    chunk = MagicMock(spec=AIMessageChunk)
    chunk.__class__ = AIMessageChunk
    chunk.text = text
    chunk.response_metadata = {"finish_reason": finish_reason} if finish_reason else {}
    chunk.tool_call_chunks = tool_call_chunks or []
    return chunk


def _make_tool_message():
    from langchain_core.messages import ToolMessage

    msg = MagicMock(spec=ToolMessage)
    msg.__class__ = ToolMessage
    return msg


# ---------------------------------------------------------------------------
# StreamState + handle_token tests
# ---------------------------------------------------------------------------


class TestHandleToken:
    def test_text_token(self):
        state = StreamState()
        chunk = _make_ai_chunk(text="hello")
        result = handle_token(chunk, state)

        assert result == ["hello"]
        assert state.full_content == "hello"
        assert state.agent_token_count == 1

    def test_empty_text_token(self):
        state = StreamState()
        chunk = _make_ai_chunk(text="")
        result = handle_token(chunk, state)

        assert result == []
        assert state.full_content == ""
        assert state.agent_token_count == 0

    def test_content_filter(self):
        state = StreamState()
        chunk = _make_ai_chunk(finish_reason="content_filter")
        result = handle_token(chunk, state)

        assert is_content_filter(result)
        assert not is_length_truncated(result)

    def test_length_truncated(self):
        state = StreamState()
        chunk = _make_ai_chunk(finish_reason="length")
        result = handle_token(chunk, state)

        assert is_length_truncated(result)
        assert not is_content_filter(result)
        assert result == ["(后续内容被截断)"]

    def test_tool_call_boundary(self):
        state = StreamState()
        handle_token(_make_ai_chunk(text="before"), state)
        chunk = _make_ai_chunk(text="", tool_call_chunks=[{"name": "search"}])
        result = handle_token(chunk, state)

        assert result == [SPLIT_MARKER]
        assert state._has_text_in_current_turn is False

    def test_tool_call_same_chunk_as_text(self):
        state = StreamState()
        handle_token(_make_ai_chunk(text="a"), state)
        chunk = _make_ai_chunk(text="b", tool_call_chunks=[{"name": "search"}])
        result = handle_token(chunk, state)

        assert result == ["b", SPLIT_MARKER]
        assert state.full_content == "ab"

    def test_tool_message(self):
        state = StreamState()
        state._has_text_in_current_turn = True
        result = handle_token(_make_tool_message(), state)

        assert result == []
        assert state.tool_call_count == 1
        assert state._has_text_in_current_turn is False

    def test_unknown_token_type(self):
        state = StreamState()
        assert handle_token("unknown", state) == []

    def test_accumulation(self):
        state = StreamState()
        for word in ["hello", " ", "world"]:
            handle_token(_make_ai_chunk(text=word), state)

        assert state.full_content == "hello world"
        assert state.agent_token_count == 3

    def test_no_split_without_prior_text(self):
        state = StreamState()
        chunk = _make_ai_chunk(text="", tool_call_chunks=[{"name": "search"}])
        result = handle_token(chunk, state)
        assert SPLIT_MARKER not in result


class TestStreamState:
    def test_defaults(self):
        state = StreamState()
        assert state.full_content == ""
        assert state.agent_token_count == 0
        assert state.tool_call_count == 0
        assert state._has_text_in_current_turn is False
