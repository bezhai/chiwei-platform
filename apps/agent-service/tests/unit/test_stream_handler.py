"""stream_handler 模块测试

测试 handle_token 对各种 token 类型的处理：
- AIMessageChunk 文本 token
- AIMessageChunk content_filter / length
- AIMessageChunk tool_call 边界 → SPLIT_MARKER
- ToolMessage
"""

from unittest.mock import MagicMock

import pytest

from app.agents.domains.main.stream_handler import (
    SPLIT_MARKER,
    StreamState,
    handle_token,
    is_content_filter,
    is_length_truncated,
)


def _make_ai_chunk(text: str = "", finish_reason: str | None = None, tool_call_chunks: list | None = None):
    """创建模拟的 AIMessageChunk"""
    from langchain.messages import AIMessageChunk
    chunk = MagicMock(spec=AIMessageChunk)
    chunk.__class__ = AIMessageChunk
    chunk.text = text
    chunk.response_metadata = {"finish_reason": finish_reason} if finish_reason else {}
    chunk.tool_call_chunks = tool_call_chunks or []
    return chunk


def _make_tool_message():
    """创建模拟的 ToolMessage"""
    from langchain.messages import ToolMessage
    msg = MagicMock(spec=ToolMessage)
    msg.__class__ = ToolMessage
    return msg


class TestHandleToken:
    def test_text_token(self):
        """普通文本 token 应返回 [text] 并更新 state"""
        state = StreamState()
        chunk = _make_ai_chunk(text="hello")
        result = handle_token(chunk, state)

        assert result == ["hello"]
        assert state.full_content == "hello"
        assert state.agent_token_count == 1

    def test_empty_text_token(self):
        """空文本 token 应返回空列表"""
        state = StreamState()
        chunk = _make_ai_chunk(text="")
        result = handle_token(chunk, state)

        assert result == []
        assert state.full_content == ""
        assert state.agent_token_count == 0

    def test_content_filter(self):
        """content_filter 应返回 [None] 信号"""
        state = StreamState()
        chunk = _make_ai_chunk(finish_reason="content_filter")
        result = handle_token(chunk, state)

        assert is_content_filter(result)
        assert not is_length_truncated(result)

    def test_length_truncated(self):
        """length 截断应返回截断提示"""
        state = StreamState()
        chunk = _make_ai_chunk(finish_reason="length")
        result = handle_token(chunk, state)

        assert is_length_truncated(result)
        assert not is_content_filter(result)
        assert result == ["(后续内容被截断)"]

    def test_tool_call_boundary_with_text(self):
        """同一 chunk 有 text 和 tool_call_chunks → 先 text 再 SPLIT_MARKER"""
        state = StreamState()

        # 先发一个文本 token，设置 _has_text_in_current_turn
        chunk1 = _make_ai_chunk(text="before")
        handle_token(chunk1, state)

        # 再发一个有 tool_call_chunks 的 chunk（无 text）
        chunk2 = _make_ai_chunk(text="", tool_call_chunks=[{"name": "search"}])
        result = handle_token(chunk2, state)

        assert result == [SPLIT_MARKER]
        assert state._has_text_in_current_turn is False

    def test_tool_call_boundary_same_chunk(self):
        """同一 chunk 同时有 text 和 tool_call_chunks"""
        state = StreamState()

        # 先设置 has_text
        chunk1 = _make_ai_chunk(text="a")
        handle_token(chunk1, state)

        # 同一 chunk 有 text + tool_call
        chunk2 = _make_ai_chunk(text="b", tool_call_chunks=[{"name": "search"}])
        result = handle_token(chunk2, state)

        assert result == ["b", SPLIT_MARKER]
        assert state.full_content == "ab"

    def test_tool_message(self):
        """ToolMessage 应递增 tool_call_count 并重置 has_text"""
        state = StreamState()
        state._has_text_in_current_turn = True

        msg = _make_tool_message()
        result = handle_token(msg, state)

        assert result == []
        assert state.tool_call_count == 1
        assert state._has_text_in_current_turn is False

    def test_unknown_token_type(self):
        """未知 token 类型应返回空列表"""
        state = StreamState()
        result = handle_token("unknown", state)

        assert result == []

    def test_multiple_text_tokens_accumulate(self):
        """多个文本 token 应累计 full_content 和 token_count"""
        state = StreamState()
        for word in ["hello", " ", "world"]:
            chunk = _make_ai_chunk(text=word)
            handle_token(chunk, state)

        assert state.full_content == "hello world"
        assert state.agent_token_count == 3

    def test_no_split_without_prior_text(self):
        """没有先发文本的 tool_call_chunks 不应产生 SPLIT_MARKER"""
        state = StreamState()
        chunk = _make_ai_chunk(text="", tool_call_chunks=[{"name": "search"}])
        result = handle_token(chunk, state)

        assert SPLIT_MARKER not in result


class TestStreamState:
    def test_default_values(self):
        state = StreamState()
        assert state.full_content == ""
        assert state.agent_token_count == 0
        assert state.tool_call_count == 0
        assert state._has_text_in_current_turn is False
