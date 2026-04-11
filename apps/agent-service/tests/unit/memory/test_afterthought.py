"""Tests for the afterthought two-phase lock behaviour.

Mirrors the old test_afterthought.py but imports from ``app.memory``.
"""

import asyncio
from unittest.mock import AsyncMock, patch

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

    on_event_called = asyncio.Event()

    async def track_on_event(chat_id, persona_id):
        on_event_called.set()

    async def inject_events_during_phase2(chat_id, persona_id):
        mgr._buffers[key] = 2

    with patch(
        "app.memory.afterthought._generate_fragment",
        new_callable=AsyncMock,
        side_effect=inject_events_during_phase2,
    ):
        with patch.object(mgr, "on_event", side_effect=track_on_event):
            await mgr._enter_phase2("chat_1", "akao")
            await asyncio.sleep(0.01)

    assert on_event_called.is_set()


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
