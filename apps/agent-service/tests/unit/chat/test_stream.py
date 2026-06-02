"""Tests for app.chat.stream — StreamState + handle_token over neutral chunks.

Post-cutover, ``handle_token`` consumes the neutral ``StreamChunk`` (five
fields: text / reasoning / finish_reason / tool_call / tool_result) instead of
langchain ``AIMessageChunk`` / ``ToolMessage``. The branch behaviour is
preserved:

  - ``finish_reason == "content_filter"`` -> ``[None]`` signal,
  - ``finish_reason == "length"``         -> truncation signal,
  - ``text``                              -> accumulate + yield,
  - ``tool_call`` after text in this turn -> SPLIT_MARKER,
  - ``tool_result``                       -> silently counted,
  - ``reasoning`` / empty                 -> nothing yielded.
"""

from app.agent.neutral import StreamChunk, ToolCall, ToolResult
from app.chat.stream import (
    SPLIT_MARKER,
    StreamState,
    handle_token,
    is_content_filter,
    is_length_truncated,
)


# ---------------------------------------------------------------------------
# StreamState + handle_token tests
# ---------------------------------------------------------------------------


class TestHandleToken:
    def test_text_token(self):
        state = StreamState()
        result = handle_token(StreamChunk(text="hello"), state)

        assert result == ["hello"]
        assert state.full_content == "hello"
        assert state.agent_token_count == 1

    def test_empty_text_token(self):
        state = StreamState()
        result = handle_token(StreamChunk(text=""), state)

        assert result == []
        assert state.full_content == ""
        assert state.agent_token_count == 0

    def test_reasoning_chunk_yields_nothing(self):
        state = StreamState()
        result = handle_token(StreamChunk(reasoning="thinking..."), state)

        assert result == []
        assert state.full_content == ""
        assert state.agent_token_count == 0

    def test_content_filter(self):
        state = StreamState()
        result = handle_token(StreamChunk(finish_reason="content_filter"), state)

        assert is_content_filter(result)
        assert not is_length_truncated(result)

    def test_length_truncated(self):
        state = StreamState()
        result = handle_token(StreamChunk(finish_reason="length"), state)

        assert is_length_truncated(result)
        assert not is_content_filter(result)
        assert result == ["(后续内容被截断)"]

    def test_stop_finish_reason_yields_nothing(self):
        state = StreamState()
        result = handle_token(StreamChunk(finish_reason="stop"), state)
        assert result == []

    def test_tool_call_boundary(self):
        state = StreamState()
        handle_token(StreamChunk(text="before"), state)
        call = ToolCall(id="c1", name="search", arguments={})
        result = handle_token(StreamChunk(tool_call=call), state)

        assert result == [SPLIT_MARKER]
        assert state._has_text_in_current_turn is False

    def test_tool_result_counted_silently(self):
        state = StreamState()
        state._has_text_in_current_turn = True
        tr = ToolResult(tool_call_id="c1", content="ok")
        result = handle_token(StreamChunk(tool_result=tr), state)

        assert result == []
        assert state.tool_call_count == 1
        assert state._has_text_in_current_turn is False

    def test_unknown_empty_chunk(self):
        state = StreamState()
        assert handle_token(StreamChunk(), state) == []

    def test_accumulation(self):
        state = StreamState()
        for word in ["hello", " ", "world"]:
            handle_token(StreamChunk(text=word), state)

        assert state.full_content == "hello world"
        assert state.agent_token_count == 3

    def test_no_split_without_prior_text(self):
        state = StreamState()
        call = ToolCall(id="c1", name="search", arguments={})
        result = handle_token(StreamChunk(tool_call=call), state)
        assert SPLIT_MARKER not in result


class TestStreamState:
    def test_defaults(self):
        state = StreamState()
        assert state.full_content == ""
        assert state.agent_token_count == 0
        assert state.tool_call_count == 0
        assert state._has_text_in_current_turn is False
