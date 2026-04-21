"""Test build_inner_context v4 — section composition only.

Section internals are covered by tests/unit/memory/sections/. These tests verify
the composer passes correct args and concatenates non-empty sections."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.memory.context import build_inner_context


@pytest.mark.asyncio
async def test_p2p_assembles_all_sections():
    # cross_chat is lazily imported inside the function body (avoids circular import),
    # so we patch the source module rather than the context module namespace.
    with (
        patch("app.memory.context.build_schedule_section", new=AsyncMock(return_value="SCHED")),
        patch("app.memory.context.build_self_abstracts_section", new=AsyncMock(return_value="SELF")),
        patch("app.memory.context.build_user_abstracts_section", new=AsyncMock(return_value="USER")),
        patch("app.memory.context.build_active_notes_section", new=AsyncMock(return_value="NOTES")),
        patch("app.memory.context.build_short_term_fragments_section", new=AsyncMock(return_value="FRAG")),
        patch("app.memory.context.build_recall_index_section", new=AsyncMock(return_value="RECALL")),
        patch("app.memory.cross_chat.build_cross_chat_context", new=AsyncMock(return_value="CROSS")),
        patch("app.memory.context._build_life_state", new=AsyncMock(return_value="LIFE")),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )
    for token in ("LIFE", "SELF", "USER", "SCHED", "NOTES", "FRAG", "RECALL", "CROSS"):
        assert token in out


@pytest.mark.asyncio
async def test_proactive_skips_cross_chat():
    """When is_proactive=True with no trigger_user, cross-chat must not be called."""
    cross_mock = AsyncMock(return_value="CROSS")
    with (
        patch("app.memory.context.build_schedule_section", new=AsyncMock(return_value="")),
        patch("app.memory.context.build_self_abstracts_section", new=AsyncMock(return_value="SELF")),
        patch("app.memory.context.build_user_abstracts_section", new=AsyncMock(return_value="")),
        patch("app.memory.context.build_active_notes_section", new=AsyncMock(return_value="")),
        patch("app.memory.context.build_short_term_fragments_section", new=AsyncMock(return_value="")),
        patch("app.memory.context.build_recall_index_section", new=AsyncMock(return_value="")),
        patch("app.memory.cross_chat.build_cross_chat_context", new=cross_mock),
        patch("app.memory.context._build_life_state", new=AsyncMock(return_value="")),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="group",
            user_ids=[], trigger_user_id=None,
            trigger_username=None, persona_id="chiwei",
            is_proactive=True,
        )
    assert "SELF" in out
    cross_mock.assert_not_called()


@pytest.mark.asyncio
async def test_sentinel_trigger_user_is_normalized():
    """trigger_user_id='__proactive__' should not trigger cross-chat or user_abstracts."""
    user_abs_mock = AsyncMock(return_value="")
    cross_mock = AsyncMock(return_value="CROSS")
    with (
        patch("app.memory.context.build_schedule_section", new=AsyncMock(return_value="")),
        patch("app.memory.context.build_self_abstracts_section", new=AsyncMock(return_value="")),
        patch("app.memory.context.build_user_abstracts_section", new=user_abs_mock),
        patch("app.memory.context.build_active_notes_section", new=AsyncMock(return_value="")),
        patch("app.memory.context.build_short_term_fragments_section", new=AsyncMock(return_value="")),
        patch("app.memory.context.build_recall_index_section", new=AsyncMock(return_value="")),
        patch("app.memory.cross_chat.build_cross_chat_context", new=cross_mock),
        patch("app.memory.context._build_life_state", new=AsyncMock(return_value="")),
    ):
        await build_inner_context(
            chat_id="oc_a", chat_type="group",
            user_ids=[], trigger_user_id="__proactive__",
            trigger_username=None, persona_id="chiwei",
        )
    # user_abstracts was called but with None → verify the effective id was None
    assert user_abs_mock.call_args.kwargs["trigger_user_id"] is None
    cross_mock.assert_not_called()
