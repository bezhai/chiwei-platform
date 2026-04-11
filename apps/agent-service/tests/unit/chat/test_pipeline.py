"""Tests for app.chat.pipeline — stream_chat, _buffer_until_pre, stream handling.

Covers:
  - Stream state tracking (token counting, content accumulation)
  - Content filter and length truncation signals
  - Tool call boundary split marker injection
  - Buffer-until-pre race logic (pass, block, exception, stream-ends-first)
"""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from app.chat.pipeline import _buffer_until_pre
from app.chat.safety import PreCheckResult
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
    chunk.response_metadata = (
        {"finish_reason": finish_reason} if finish_reason else {}
    )
    chunk.tool_call_chunks = tool_call_chunks or []
    return chunk


def _make_tool_message():
    from langchain_core.messages import ToolMessage

    msg = MagicMock(spec=ToolMessage)
    msg.__class__ = ToolMessage
    return msg


async def _fake_stream(*tokens: str) -> AsyncGenerator[str, None]:
    for t in tokens:
        yield t


async def _delayed_stream(
    *tokens: str, delay: float = 0.05
) -> AsyncGenerator[str, None]:
    for t in tokens:
        await asyncio.sleep(delay)
        yield t


def _make_pre_task(
    is_blocked: bool, block_reason: str = "", delay: float = 0
) -> asyncio.Task:
    async def _pre():
        if delay:
            await asyncio.sleep(delay)
        return PreCheckResult(is_blocked=is_blocked, block_reason=block_reason)

    return asyncio.create_task(_pre())


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


# ---------------------------------------------------------------------------
# _buffer_until_pre race tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_passes_flush_buffer():
    stream = _delayed_stream("a", "b", "c", delay=0.02)
    pre_task = _make_pre_task(is_blocked=False, delay=0.05)

    result = []
    async for text in _buffer_until_pre(stream, pre_task, "msg-1"):
        result.append(text)

    assert result == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_pre_blocks_yield_guard():
    stream = _delayed_stream("a", "b", "c", delay=0.02)
    pre_task = _make_pre_task(is_blocked=True, block_reason="nsfw", delay=0.05)

    result = []
    async for text in _buffer_until_pre(stream, pre_task, "msg-2", guard_message="blocked!"):
        result.append(text)

    assert result == ["blocked!"]


@pytest.mark.asyncio
async def test_pre_passes_immediately():
    stream = _delayed_stream("x", "y", delay=0.05)
    pre_task = _make_pre_task(is_blocked=False, delay=0)

    result = []
    async for text in _buffer_until_pre(stream, pre_task, "msg-3"):
        result.append(text)

    assert result == ["x", "y"]


@pytest.mark.asyncio
async def test_stream_ends_before_pre_passes():
    stream = _fake_stream("hello")
    pre_task = _make_pre_task(is_blocked=False, delay=0.1)

    result = []
    async for text in _buffer_until_pre(stream, pre_task, "msg-4"):
        result.append(text)

    assert result == ["hello"]


@pytest.mark.asyncio
async def test_stream_ends_before_pre_blocks():
    stream = _fake_stream("hello")
    pre_task = _make_pre_task(is_blocked=True, block_reason="harmful", delay=0.1)

    result = []
    async for text in _buffer_until_pre(stream, pre_task, "msg-5", guard_message="nope"):
        result.append(text)

    assert result == ["nope"]


@pytest.mark.asyncio
async def test_empty_stream_pre_passes():
    stream = _fake_stream()
    pre_task = _make_pre_task(is_blocked=False, delay=0.01)

    result = []
    async for text in _buffer_until_pre(stream, pre_task, "msg-6"):
        result.append(text)

    assert result == []


@pytest.mark.asyncio
async def test_pre_exception_flush_buffer():
    """Pre task exception -> flush buffer (don't crash)."""
    stream = _fake_stream("a", "b")

    async def _failing_pre():
        await asyncio.sleep(0.1)
        raise ValueError("pre failed")

    pre_task = asyncio.create_task(_failing_pre())

    result = []
    async for text in _buffer_until_pre(stream, pre_task, "msg-7"):
        result.append(text)

    assert result == ["a", "b"]
