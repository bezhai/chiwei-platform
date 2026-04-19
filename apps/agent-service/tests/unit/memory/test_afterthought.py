"""Tests for the afterthought two-phase lock behaviour.

Mirrors the old test_afterthought.py but imports from ``app.memory``.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.afterthought import _Afterthought


def _make() -> _Afterthought:
    """Fresh instance (not the module-level singleton)."""
    return _Afterthought()


# ---------------------------------------------------------------------------
# on_event starts phase 1 timer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_event_starts_phase1_timer():
    mgr = _make()

    with patch.object(mgr, "_phase1_timer", new_callable=AsyncMock) as mock_timer:
        mock_timer.return_value = None
        await mgr.on_event("chat_1", "akao")

    key = "chat_1:akao"
    assert key in mgr._buffers
    assert mgr._buffers[key] == 1
    assert key in mgr._timers


# ---------------------------------------------------------------------------
# Multiple events reset the debounce timer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_events_reset_debounce_timer():
    mgr = _make()
    key = "chat_1:akao"

    await mgr.on_event("chat_1", "akao")
    first_timer = mgr._timers.get(key)
    assert first_timer is not None

    await mgr.on_event("chat_1", "akao")
    second_timer = mgr._timers.get(key)

    await asyncio.sleep(0.01)

    assert first_timer.cancelled()
    assert second_timer is not first_timer
    assert mgr._buffers[key] == 2

    if second_timer and not second_timer.done():
        second_timer.cancel()


# ---------------------------------------------------------------------------
# Buffer exceeding max forces phase 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buffer_exceeding_max_forces_phase2():
    mgr = _make()
    mgr._max_buffer = 3
    key = "chat_1:akao"

    phase2_called = asyncio.Event()

    async def mock_enter_phase2(chat_id, persona_id):
        phase2_called.set()

    with patch.object(mgr, "_enter_phase2", side_effect=mock_enter_phase2):
        await mgr.on_event("chat_1", "akao")
        await mgr.on_event("chat_1", "akao")
        if key in mgr._timers:
            mgr._timers[key].cancel()
        await mgr.on_event("chat_1", "akao")

    await asyncio.sleep(0.01)
    assert phase2_called.is_set()


# ---------------------------------------------------------------------------
# Phase 2 running blocks new triggers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase2_running_blocks_new_phase2():
    mgr = _make()
    key = "chat_1:akao"
    mgr._phase2_running.add(key)

    await mgr.on_event("chat_1", "akao")
    await mgr.on_event("chat_1", "akao")

    assert mgr._buffers[key] == 2
    assert key not in mgr._timers


# ---------------------------------------------------------------------------
# Phase 2 calls _generate_fragment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase2_calls_generate_fragment():
    mgr = _make()
    key = "chat_1:akao"
    mgr._buffers[key] = 5

    with patch(
        "app.memory.afterthought._generate_fragment",
        new_callable=AsyncMock,
    ) as mock_gen:
        await mgr._enter_phase2("chat_1", "akao")

    mock_gen.assert_awaited_once_with("chat_1", "akao")
    assert key not in mgr._phase2_running


# ---------------------------------------------------------------------------
# Phase 2 triggers next cycle when buffer is non-empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase2_triggers_next_cycle_if_buffer_has_events():
    mgr = _make()
    key = "chat_1:akao"
    mgr._buffers[key] = 3

    async def inject_events_during_phase2(chat_id, persona_id):
        mgr._buffers[key] = 2

    with patch(
        "app.memory.afterthought._generate_fragment",
        new_callable=AsyncMock,
        side_effect=inject_events_during_phase2,
    ):
        await mgr._enter_phase2("chat_1", "akao")
        await asyncio.sleep(0.01)

    # After phase2, remaining buffer should start a new debounce timer
    assert key in mgr._timers
    # Buffer count should be preserved (not inflated by phantom +1)
    assert mgr._buffers.get(key, 0) == 2


# ---------------------------------------------------------------------------
# Phase 2 error cleans up state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase2_error_cleans_up():
    mgr = _make()
    key = "chat_1:akao"
    mgr._buffers[key] = 3

    with patch(
        "app.memory.afterthought._generate_fragment",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM down"),
    ):
        await mgr._enter_phase2("chat_1", "akao")

    assert key not in mgr._phase2_running
    assert key not in mgr._buffers


# ---------------------------------------------------------------------------
# _generate_fragment — v4 write path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_fragment_writes_to_new_table_and_enqueues_vectorize():
    """_generate_fragment should write a v4 Fragment (source='afterthought')
    and enqueue fragment vectorize."""
    from app.memory.afterthought import _generate_fragment

    fake_message = MagicMock(role="user", user_id="u1", chat_type="p2p")
    with patch(
        "app.memory.afterthought.find_messages_in_range",
        new=AsyncMock(return_value=[fake_message]),
    ):
        with patch(
            "app.memory.afterthought.load_persona",
            new=AsyncMock(return_value=MagicMock(display_name="ayana", persona_lite="x")),
        ):
            with patch(
                "app.memory.afterthought._build_scene",
                new=AsyncMock(return_value="scene"),
            ):
                with patch(
                    "app.memory.afterthought.format_timeline",
                    new=AsyncMock(return_value="t"),
                ):
                    with patch("app.memory.afterthought.Agent") as MockAgent:
                        MockAgent.return_value.run = AsyncMock(
                            return_value=MagicMock(content="hello world")
                        )
                        with patch(
                            "app.memory.afterthought.extract_text",
                            return_value="this is the generated content",
                        ):
                            with patch(
                                "app.memory.afterthought.insert_fragment",
                                new=AsyncMock(),
                            ) as mock_ins:
                                with patch(
                                    "app.memory.afterthought.enqueue_fragment_vectorize",
                                    new=AsyncMock(),
                                ) as mock_enq:
                                    with patch(
                                        "app.memory.afterthought.get_session",
                                    ) as mock_session:
                                        mock_ctx = AsyncMock()
                                        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
                                        mock_ctx.__aexit__ = AsyncMock(return_value=False)
                                        mock_session.return_value = mock_ctx
                                        with patch(
                                            "app.memory.relationships.extract_relationship_updates",
                                            new=AsyncMock(),
                                        ):
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
    from app.memory.afterthought import _generate_fragment

    with patch(
        "app.memory.afterthought.get_session",
    ) as mock_session:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_ctx
        with patch(
            "app.memory.afterthought.find_messages_in_range",
            new=AsyncMock(return_value=[]),
        ):
            with patch(
                "app.memory.afterthought.insert_fragment", new=AsyncMock()
            ) as mock_ins:
                with patch(
                    "app.memory.afterthought.enqueue_fragment_vectorize", new=AsyncMock()
                ) as mock_enq:
                    await _generate_fragment("chat_1", "ayana")

    mock_ins.assert_not_awaited()
    mock_enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_fragment_skip_when_empty_content():
    """_generate_fragment should return early without insert when LLM returns empty."""
    from app.memory.afterthought import _generate_fragment

    fake_message = MagicMock(role="user", user_id="u1", chat_type="p2p")
    with patch(
        "app.memory.afterthought.find_messages_in_range",
        new=AsyncMock(return_value=[fake_message]),
    ):
        with patch(
            "app.memory.afterthought.load_persona",
            new=AsyncMock(return_value=MagicMock(display_name="ayana", persona_lite="x")),
        ):
            with patch(
                "app.memory.afterthought._build_scene",
                new=AsyncMock(return_value="scene"),
            ):
                with patch(
                    "app.memory.afterthought.format_timeline",
                    new=AsyncMock(return_value="t"),
                ):
                    with patch("app.memory.afterthought.Agent") as MockAgent:
                        MockAgent.return_value.run = AsyncMock(
                            return_value=MagicMock(content="")
                        )
                        with patch(
                            "app.memory.afterthought.extract_text",
                            return_value="",
                        ):
                            with patch(
                                "app.memory.afterthought.insert_fragment", new=AsyncMock()
                            ) as mock_ins:
                                with patch(
                                    "app.memory.afterthought.enqueue_fragment_vectorize",
                                    new=AsyncMock(),
                                ) as mock_enq:
                                    with patch(
                                        "app.memory.afterthought.get_session",
                                    ) as mock_session:
                                        mock_ctx = AsyncMock()
                                        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
                                        mock_ctx.__aexit__ = AsyncMock(return_value=False)
                                        mock_session.return_value = mock_ctx
                                        await _generate_fragment("chat_1", "ayana")

    mock_ins.assert_not_awaited()
    mock_enq.assert_not_awaited()
