"""Test build_inner_context — scene + life snapshot only.

After the chat-input rewrite, inner_context is exactly two sections:
the scene and the life "right now" snapshot. None of the old RAG sections
(self/user abstracts, active notes, cross-chat, short-term fragments,
recall index, schedule) are assembled anymore — those section functions
are gone. These tests pin that contract and the life-snapshot fallback."""

from __future__ import annotations

from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.memory.context import _build_life_state, build_inner_context

CST = timezone(timedelta(hours=8))


@pytest.mark.asyncio
async def test_p2p_assembles_scene_and_life_only():
    """p2p inner_context = scene + life snapshot, nothing else."""
    with patch(
        "app.memory.context._build_life_state",
        new=AsyncMock(return_value="你此刻在做饭，心情轻松"),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    # scene present
    assert "浩南" in out
    # life snapshot present
    assert "做饭" in out
    # exactly two sections joined by the blank-line separator
    assert out.count("\n\n") == 1


@pytest.mark.asyncio
async def test_no_old_rag_sections_assembled():
    """The old RAG section builders must not be imported/called anymore.

    Guards against accidental re-introduction: the names should no longer
    exist on the context module, so patching them must raise AttributeError.
    """
    import app.memory.context as ctx

    for name in (
        "build_schedule_section",
        "build_self_abstracts_section",
        "build_user_abstracts_section",
        "build_active_notes_section",
        "build_short_term_fragments_section",
        "build_recall_index_section",
    ):
        assert not hasattr(ctx, name), f"{name} should be removed from context module"


@pytest.mark.asyncio
async def test_inner_context_has_no_recall_or_relationship_text():
    """No leftover RAG phrasing (recall hint / relationship / notes) in output."""
    with patch(
        "app.memory.context._build_life_state",
        new=AsyncMock(return_value="你此刻在散步"),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    for marker in ("recall(", "关于你自己", "你们的关系", "你的清单", "抽象认识", "新鲜经历"):
        assert marker not in out


@pytest.mark.asyncio
async def test_life_snapshot_is_the_main_subject():
    """When life state exists, it leads as the snapshot of who she is right now."""
    snap = SimpleNamespace(current_state="在房间里写作业", response_mood="有点烦躁")
    with (
        patch("app.memory.context.find_life_state", new=AsyncMock(return_value=snap)),
        patch("app.memory.context.current_deployment_lane", return_value=None),
    ):
        out = await _build_life_state("chiwei")

    assert "在房间里写作业" in out
    assert "有点烦躁" in out


@pytest.mark.asyncio
async def test_build_life_state_reads_new_snapshot_by_lane():
    """_build_life_state reads the LifeState snapshot keyed by deployment lane."""
    snap = SimpleNamespace(current_state="正在做饭", response_mood="轻松")
    find = AsyncMock(return_value=snap)
    with (
        patch("app.memory.context.find_life_state", new=find),
        patch("app.memory.context.current_deployment_lane", return_value="ppe-x"),
    ):
        out = await _build_life_state("akao")

    assert "正在做饭" in out
    assert "轻松" in out
    assert find.await_args.kwargs == {"lane": "ppe-x", "persona_id": "akao"}


@pytest.mark.asyncio
async def test_build_life_state_lane_falls_back_to_prod():
    """prod (LANE unset → None) normalizes to 'prod', matching the write side."""
    snap = SimpleNamespace(current_state="看书", response_mood="安静")
    find = AsyncMock(return_value=snap)
    with (
        patch("app.memory.context.find_life_state", new=find),
        patch("app.memory.context.current_deployment_lane", return_value=None),
    ):
        out = await _build_life_state("akao")

    assert "看书" in out
    assert find.await_args.kwargs == {"lane": "prod", "persona_id": "akao"}


@pytest.mark.asyncio
async def test_life_snapshot_fallback_when_no_snapshot():
    """No snapshot yet (cold start) → a simple fallback line, never empty.

    inner_context must not collapse: chat still works even before she's
    lived a round.
    """
    with (
        patch("app.memory.context.find_life_state", new=AsyncMock(return_value=None)),
        patch("app.memory.context.current_deployment_lane", return_value=None),
    ):
        out = await _build_life_state("akao")

    assert out  # non-empty fallback
    assert "此刻" in out  # references her current state in the fallback phrasing


@pytest.mark.asyncio
async def test_life_snapshot_fallback_when_thin_current_state():
    """current_state empty/whitespace → fallback, not a half-built section."""
    snap = SimpleNamespace(current_state="   ", response_mood="平静")
    with (
        patch("app.memory.context.find_life_state", new=AsyncMock(return_value=snap)),
        patch("app.memory.context.current_deployment_lane", return_value=None),
    ):
        out = await _build_life_state("akao")

    assert out  # non-empty fallback
    assert "此刻" in out


@pytest.mark.asyncio
async def test_life_snapshot_fallback_on_error():
    """find_life_state raising → fallback, inner_context still builds."""
    with (
        patch(
            "app.memory.context.find_life_state",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch("app.memory.context.current_deployment_lane", return_value=None),
    ):
        out = await _build_life_state("akao")

    assert out  # non-empty fallback, no exception propagated


@pytest.mark.asyncio
async def test_inner_context_never_collapses_on_cold_start():
    """Full build with no snapshot still yields scene + life fallback."""
    with (
        patch("app.memory.context.find_life_state", new=AsyncMock(return_value=None)),
        patch("app.memory.context.current_deployment_lane", return_value=None),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert "浩南" in out  # scene
    assert "此刻" in out  # life fallback
