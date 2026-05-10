"""Tests for app.nodes.memory_pipelines._generate_fragment.

Migrated from tests/unit/memory/test_afterthought.py (Phase 3 Task 12).
The base-class debouncer tests (on_event / phase1 / phase2) are dropped —
the equivalent runtime behaviour is covered by tests/runtime/test_debounce.py.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.memory_request import MemoryFragmentRequest


@asynccontextmanager
async def _fake_tx():
    yield


def _make_emit_tx_mock():
    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    return _fake_emit_tx, captured


# ---------------------------------------------------------------------------
# _generate_fragment — v4 write path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_fragment_writes_to_new_table_and_enqueues_vectorize():
    """_generate_fragment should write a v4 Fragment (source='afterthought')
    and enqueue fragment vectorize."""
    from app.nodes.memory_pipelines import _generate_fragment

    fake_message = MagicMock(role="user", user_id="u1", chat_type="p2p")
    fake_emit, captured = _make_emit_tx_mock()
    with patch(
        "app.nodes.memory_pipelines.find_messages_in_range",
        new=AsyncMock(return_value=[fake_message]),
    ), patch(
        "app.nodes.memory_pipelines.load_persona",
        new=AsyncMock(return_value=MagicMock(display_name="ayana", persona_lite="x")),
    ), patch(
        "app.nodes.memory_pipelines._build_scene",
        new=AsyncMock(return_value="scene"),
    ), patch(
        "app.nodes.memory_pipelines.format_timeline",
        new=AsyncMock(return_value="t"),
    ), patch("app.nodes.memory_pipelines.Agent") as MockAgent, patch(
        "app.nodes.memory_pipelines.extract_text",
        return_value="this is the generated content",
    ), patch(
        "app.nodes.memory_pipelines.insert_fragment",
        new=AsyncMock(),
    ) as mock_ins, patch(
        "app.nodes.memory_pipelines.tx", _fake_tx
    ), patch(
        "app.nodes.memory_pipelines.emit_tx", fake_emit
    ):
        MockAgent.return_value.run = AsyncMock(
            return_value=MagicMock(content="hello world")
        )
        await _generate_fragment("chat_1", "ayana")

    mock_ins.assert_awaited_once()
    kwargs = mock_ins.call_args.kwargs
    assert kwargs["source"] == "afterthought"
    assert kwargs["chat_id"] == "chat_1"
    assert kwargs["persona_id"] == "ayana"
    assert kwargs["content"] == "this is the generated content"
    assert kwargs["id"].startswith("f_")
    assert len(captured) == 1
    emitted = captured[0]
    assert isinstance(emitted, MemoryFragmentRequest)
    assert emitted.fragment_id == kwargs["id"]


@pytest.mark.asyncio
async def test_generate_fragment_skip_when_no_messages():
    """_generate_fragment should return early without insert when messages=[]."""
    from app.nodes.memory_pipelines import _generate_fragment

    with patch(
        "app.nodes.memory_pipelines.find_messages_in_range",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.nodes.memory_pipelines.insert_fragment", new=AsyncMock()
    ) as mock_ins, patch(
        "app.nodes.memory_pipelines.emit", new=AsyncMock()
    ) as mock_enq:
        await _generate_fragment("chat_1", "ayana")

    mock_ins.assert_not_awaited()
    mock_enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_fragment_skip_when_empty_content():
    """_generate_fragment should return early without insert when LLM returns empty."""
    from app.nodes.memory_pipelines import _generate_fragment

    fake_message = MagicMock(role="user", user_id="u1", chat_type="p2p")
    with patch(
        "app.nodes.memory_pipelines.find_messages_in_range",
        new=AsyncMock(return_value=[fake_message]),
    ), patch(
        "app.nodes.memory_pipelines.load_persona",
        new=AsyncMock(return_value=MagicMock(display_name="ayana", persona_lite="x")),
    ), patch(
        "app.nodes.memory_pipelines._build_scene",
        new=AsyncMock(return_value="scene"),
    ), patch(
        "app.nodes.memory_pipelines.format_timeline",
        new=AsyncMock(return_value="t"),
    ), patch("app.nodes.memory_pipelines.Agent") as MockAgent, patch(
        "app.nodes.memory_pipelines.extract_text",
        return_value="",
    ), patch(
        "app.nodes.memory_pipelines.insert_fragment", new=AsyncMock()
    ) as mock_ins, patch(
        "app.nodes.memory_pipelines.emit",
        new=AsyncMock(),
    ) as mock_enq:
        MockAgent.return_value.run = AsyncMock(
            return_value=MagicMock(content="")
        )
        await _generate_fragment("chat_1", "ayana")

    mock_ins.assert_not_awaited()
    mock_enq.assert_not_awaited()
