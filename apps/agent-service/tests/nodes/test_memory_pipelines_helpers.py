"""Tests for app.nodes.memory_pipelines._generate_fragment.

Migrated from tests/unit/memory/test_afterthought.py (Phase 3 Task 12).
The base-class debouncer tests (on_event / phase1 / phase2) are dropped —
the equivalent runtime behaviour is covered by tests/runtime/test_debounce.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _generate_fragment — v4 write path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_fragment_writes_to_new_table_and_enqueues_vectorize():
    """_generate_fragment should write a v4 Fragment (source='afterthought')
    and enqueue fragment vectorize."""
    from app.nodes.memory_pipelines import _generate_fragment

    fake_message = MagicMock(role="user", user_id="u1", chat_type="p2p")
    with patch(
        "app.nodes.memory_pipelines.find_messages_in_range",
        new=AsyncMock(return_value=[fake_message]),
    ):
        with patch(
            "app.nodes.memory_pipelines.load_persona",
            new=AsyncMock(return_value=MagicMock(display_name="ayana", persona_lite="x")),
        ):
            with patch(
                "app.nodes.memory_pipelines._build_scene",
                new=AsyncMock(return_value="scene"),
            ):
                with patch(
                    "app.nodes.memory_pipelines.format_timeline",
                    new=AsyncMock(return_value="t"),
                ):
                    with patch("app.nodes.memory_pipelines.Agent") as MockAgent:
                        MockAgent.return_value.run = AsyncMock(
                            return_value=MagicMock(content="hello world")
                        )
                        with patch(
                            "app.nodes.memory_pipelines.extract_text",
                            return_value="this is the generated content",
                        ):
                            with patch(
                                "app.nodes.memory_pipelines.insert_fragment",
                                new=AsyncMock(),
                            ) as mock_ins:
                                with patch(
                                    "app.nodes.memory_pipelines.enqueue_fragment_vectorize",
                                    new=AsyncMock(),
                                ) as mock_enq:
                                    with patch(
                                        "app.nodes.memory_pipelines.get_session",
                                    ) as mock_session:
                                        mock_ctx = AsyncMock()
                                        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
                                        mock_ctx.__aexit__ = AsyncMock(return_value=False)
                                        mock_session.return_value = mock_ctx
                                        await _generate_fragment("chat_1", "ayana")

    mock_ins.assert_awaited_once()
    kwargs = mock_ins.call_args.kwargs
    assert kwargs["source"] == "afterthought"
    assert kwargs["chat_id"] == "chat_1"
    assert kwargs["persona_id"] == "ayana"
    assert kwargs["content"] == "this is the generated content"
    assert kwargs["id"].startswith("f_")
    mock_enq.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_fragment_skip_when_no_messages():
    """_generate_fragment should return early without insert when messages=[]."""
    from app.nodes.memory_pipelines import _generate_fragment

    with patch(
        "app.nodes.memory_pipelines.get_session",
    ) as mock_session:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_ctx
        with patch(
            "app.nodes.memory_pipelines.find_messages_in_range",
            new=AsyncMock(return_value=[]),
        ):
            with patch(
                "app.nodes.memory_pipelines.insert_fragment", new=AsyncMock()
            ) as mock_ins:
                with patch(
                    "app.nodes.memory_pipelines.enqueue_fragment_vectorize", new=AsyncMock()
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
    ):
        with patch(
            "app.nodes.memory_pipelines.load_persona",
            new=AsyncMock(return_value=MagicMock(display_name="ayana", persona_lite="x")),
        ):
            with patch(
                "app.nodes.memory_pipelines._build_scene",
                new=AsyncMock(return_value="scene"),
            ):
                with patch(
                    "app.nodes.memory_pipelines.format_timeline",
                    new=AsyncMock(return_value="t"),
                ):
                    with patch("app.nodes.memory_pipelines.Agent") as MockAgent:
                        MockAgent.return_value.run = AsyncMock(
                            return_value=MagicMock(content="")
                        )
                        with patch(
                            "app.nodes.memory_pipelines.extract_text",
                            return_value="",
                        ):
                            with patch(
                                "app.nodes.memory_pipelines.insert_fragment", new=AsyncMock()
                            ) as mock_ins:
                                with patch(
                                    "app.nodes.memory_pipelines.enqueue_fragment_vectorize",
                                    new=AsyncMock(),
                                ) as mock_enq:
                                    with patch(
                                        "app.nodes.memory_pipelines.get_session",
                                    ) as mock_session:
                                        mock_ctx = AsyncMock()
                                        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
                                        mock_ctx.__aexit__ = AsyncMock(return_value=False)
                                        mock_session.return_value = mock_ctx
                                        await _generate_fragment("chat_1", "ayana")

    mock_ins.assert_not_awaited()
    mock_enq.assert_not_awaited()
