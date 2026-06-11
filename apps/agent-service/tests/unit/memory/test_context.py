"""Test build_inner_context — world-arc awareness + scene + life snapshot.

After the chat-input rewrite, inner_context is the scene and the life
"right now" snapshot; the world-arc passthrough adds a third, leading
section **only when the arc exists** (cold chain → absent, no placeholder).
None of the old RAG sections (self/user abstracts, active notes, cross-chat,
short-term fragments, recall index, schedule) are assembled anymore — those
section functions are gone. These tests pin that contract, the life-snapshot
fallback, and the arc passthrough (presence / absence / ordering / lane)."""

from __future__ import annotations

from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.memory.context import _build_life_state, build_inner_context

CST = timezone(timedelta(hours=8))


def _no_arc():
    """Stub the arc read to a cold chain (None) so unit tests stay DB-free."""
    return patch(
        "app.domain.arc_awareness.read_world_arc", new=AsyncMock(return_value=None)
    )


@pytest.mark.asyncio
async def test_p2p_assembles_scene_and_life_only():
    """p2p inner_context with a cold arc chain = scene + life snapshot, nothing else."""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在做饭，心情轻松"),
        ),
        _no_arc(),
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
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
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
        _no_arc(),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert "浩南" in out  # scene
    assert "此刻" in out  # life fallback


# ---------------------------------------------------------------------------
# 世界阶段透传：chat 的 inner_context 带「你们一家所处的现实阶段」段
# （对话里她也必须知道自己人生走到哪页 —— 与 life 每轮唤醒同一份渲染。）
# ---------------------------------------------------------------------------

_ARC_HEADER_MARK = "【你们一家所处的现实阶段】"


def _arc_read(narrative: str | None):
    """Stub the arc read; ``narrative=None`` 表示空链，否则返回一版 WorldArc。"""
    from app.world.arc import WorldArc

    arc = (
        None
        if narrative is None
        else WorldArc(
            lane="prod", narrative=narrative, turned_at="2026-06-09T18:00:00+08:00"
        )
    )
    return patch(
        "app.domain.arc_awareness.read_world_arc", new=AsyncMock(return_value=arc)
    )


@pytest.mark.asyncio
async def test_arc_awareness_leads_inner_context_when_arc_exists():
    """有世界阶段 → inner_context 带阶段段，且排在 scene 与 life 快照之前。

    缓存前缀原则：世界阶段天/周级才变，必须在每条消息都变的 scene、小时级变的
    life 快照之前（稳定前缀区），不插在每轮都变的内容后面。
    """
    narrative = "一家人刚搬过来，老二换了新学校，眼下是初夏。"
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在做饭，心情轻松"),
        ),
        _arc_read(narrative),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _ARC_HEADER_MARK in out, "有世界阶段时 inner_context 必须带阶段段"
    assert narrative in out, "阶段全文必须原样进 inner_context"
    arc_pos = out.index(_ARC_HEADER_MARK)
    assert arc_pos < out.index("浩南"), "阶段段必须排在 scene（每条消息都变）之前"
    assert arc_pos < out.index("做饭"), "阶段段必须排在 life 快照（小时级变）之前"


@pytest.mark.asyncio
async def test_cold_arc_chain_inner_context_has_no_section_no_placeholder():
    """空链 → 整段缺席、无占位文案，inner_context 仍是 scene + life 两段。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _arc_read(None),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _ARC_HEADER_MARK not in out
    assert "现实阶段" not in out, "空链时不许出现任何阶段占位文案"
    assert out.count("\n\n") == 1  # 仍是 scene + life 两段


@pytest.mark.asyncio
async def test_arc_read_uses_deployment_lane():
    """lane 口径与 _build_life_state 一致：current_deployment_lane()，prod 归一 "prod"。"""
    read = AsyncMock(return_value=None)
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value="ppe-x"),
        patch("app.domain.arc_awareness.read_world_arc", new=read),
    ):
        await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert read.await_args.kwargs == {"lane": "ppe-x"}


@pytest.mark.asyncio
async def test_arc_lane_falls_back_to_prod():
    """LANE 未设（prod → None）归一到 "prod"，与世界阶段写入端同口径。"""
    read = AsyncMock(return_value=None)
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value=None),
        patch("app.domain.arc_awareness.read_world_arc", new=read),
    ):
        await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert read.await_args.kwargs == {"lane": "prod"}


@pytest.mark.asyncio
async def test_arc_read_error_inner_context_still_builds():
    """阶段读失败 → 整段缺席但 inner_context 不塌（chat 照常对话）。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch(
            "app.domain.arc_awareness.read_world_arc",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _ARC_HEADER_MARK not in out
    assert "散步" in out  # life 快照照常
    assert "浩南" in out  # scene 照常
