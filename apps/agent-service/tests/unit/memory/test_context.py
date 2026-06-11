"""Test build_inner_context — arc awareness + scene + pages + life snapshot.

After the chat-input rewrite, inner_context is the scene and the life
"right now" snapshot; the world-arc passthrough adds a leading section
**only when the arc exists** (cold chain → absent, no placeholder). The
bedtime-review pages add two more optional sections between scene and life
snapshot: the relationship page of the person she's talking to
(trigger_user_id) and her latest day page — both absent (no placeholder)
when there is no page / no trigger user / the read fails. None of the old
RAG sections (self/user abstracts, active notes, cross-chat, short-term
fragments, recall index, schedule) are assembled anymore — those section
functions are gone. These tests pin that contract, the life-snapshot
fallback, the arc passthrough, and the page injections
(presence / absence / ordering / lane / plot-fact-free frames)."""

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


def _no_relationship_page():
    """Stub the relationship-page read to None so unit tests stay DB-free."""
    return patch(
        "app.memory.context.read_relationship_page",
        new=AsyncMock(return_value=None),
    )


def _no_day_page():
    """Stub the latest-day-page read to None so unit tests stay DB-free."""
    return patch(
        "app.memory.context.read_latest_day_page",
        new=AsyncMock(return_value=None),
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
        _no_relationship_page(),
        _no_day_page(),
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
async def test_inner_context_has_no_recall_or_rag_leftover_text():
    """No leftover RAG phrasing (recall hint / notes / abstracts) in output.

    「你们的关系」不再出现在这份禁词表里：睡前回顾把它变成了合法段名
    （关系页注入，来源是版本链不是 RAG）——无页时整段缺席由下方专门的
    缺席用例钉死。
    """
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    for marker in ("recall(", "关于你自己", "你的清单", "抽象认识", "新鲜经历"):
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
        _no_relationship_page(),
        _no_day_page(),
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
        _no_relationship_page(),
        _no_day_page(),
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
        _no_relationship_page(),
        _no_day_page(),
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
        _no_relationship_page(),
        _no_day_page(),
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
        _no_relationship_page(),
        _no_day_page(),
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
        _no_relationship_page(),
        _no_day_page(),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _ARC_HEADER_MARK not in out
    assert "散步" in out  # life 快照照常
    assert "浩南" in out  # scene 照常


# ---------------------------------------------------------------------------
# 睡前回顾两页注入：【你们的关系】（对话对象的关系页）+【你的昨天】（最近一页昨天）
# （位置：场景段之后、人生快照之前——你在和谁聊 → 你们的关系 → 你的昨天 → 你此刻状态。
#  无页 / 无触发人 / 读失败 → 整段缺席不补占位；框架文案平直第一人称、零剧情事实。）
# ---------------------------------------------------------------------------

_REL_HEADER_MARK = "【你们的关系】"
_DAY_HEADER_MARK = "【你的昨天】"


def _relationship_page(narrative: str, *, written_at: str = "2026-06-10T23:45:00+08:00"):
    from app.life.pages import RelationshipPage

    return RelationshipPage(
        lane="prod",
        persona_id="chiwei",
        other_user_id="u1",
        narrative=narrative,
        written_at=written_at,
    )


def _day_page(narrative: str, *, written_at: str = "2026-06-10T23:40:00+08:00"):
    from app.life.pages import DayPage

    return DayPage(
        lane="prod",
        persona_id="chiwei",
        date="2026-06-10",
        narrative=narrative,
        written_at=written_at,
    )


def _rel_read(page):
    return patch(
        "app.memory.context.read_relationship_page",
        new=AsyncMock(return_value=page),
    )


def _day_read(page):
    return patch(
        "app.memory.context.read_latest_day_page",
        new=AsyncMock(return_value=page),
    )


@pytest.mark.asyncio
async def test_relationship_page_injected_for_trigger_user_in_p2p():
    """p2p 有关系页 → 注入【你们的关系】段：页全文 + written_at 时间标注。"""
    narrative = "他总在深夜出现，问我今天过得怎么样。"
    read = AsyncMock(return_value=_relationship_page(narrative))
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value=None),
        _no_arc(),
        patch("app.memory.context.read_relationship_page", new=read),
        _no_day_page(),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _REL_HEADER_MARK in out, "有关系页时必须注入【你们的关系】段"
    assert narrative in out, "关系页全文必须原样进 inner_context"
    assert "这页写于" in out, "页文必须带 written_at 时间标注（让她知道记忆新旧）"
    assert "2026-06-10T23:45:00+08:00" in out
    assert read.await_args.kwargs == {
        "lane": "prod",
        "persona_id": "chiwei",
        "other_user_id": "u1",
    }, "p2p 用 trigger_user_id 查对方关系页，lane 口径 prod 归一"


@pytest.mark.asyncio
async def test_relationship_page_injected_for_trigger_user_in_group():
    """群聊也用 trigger_user_id 查关系页（spec 决策 6：群聊注触发人页）。"""
    read = AsyncMock(return_value=_relationship_page("群里认识的人。"))
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value="ppe-x"),
        _no_arc(),
        patch("app.memory.context.read_relationship_page", new=read),
        _no_day_page(),
    ):
        out = await build_inner_context(
            chat_id="oc_g", chat_type="group",
            user_ids=["u1", "u2"], trigger_user_id="u2",
            trigger_username="浩南", persona_id="chiwei",
            chat_name="小群",
        )

    assert _REL_HEADER_MARK in out
    assert read.await_args.kwargs == {
        "lane": "ppe-x",
        "persona_id": "chiwei",
        "other_user_id": "u2",
    }, "群聊也按 trigger_user_id 查页，lane 走 current_deployment_lane()"


@pytest.mark.asyncio
async def test_relationship_section_absent_when_no_trigger_user():
    """trigger_user_id 为 None（如 proactive 无触发人）→ 整段缺席且不发起读。"""
    read = AsyncMock(return_value=_relationship_page("不该被读到的页。"))
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        patch("app.memory.context.read_relationship_page", new=read),
        _no_day_page(),
    ):
        out = await build_inner_context(
            chat_id="oc_g", chat_type="group",
            user_ids=["u1"], trigger_user_id=None,
            trigger_username=None, persona_id="chiwei",
            chat_name="小群", is_proactive=True,
            proactive_stimulus="群里在聊晚饭",
        )

    assert _REL_HEADER_MARK not in out
    assert "你们的关系" not in out, "无触发人时不许出现任何关系占位文案"
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_relationship_section_absent_when_no_page():
    """触发人没有关系页（第一次聊）→ 整段缺席、不补占位。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _REL_HEADER_MARK not in out
    assert "你们的关系" not in out


@pytest.mark.asyncio
async def test_latest_day_page_injected():
    """有最近一页昨天 → 注入【你的昨天】段：页全文 + written_at 时间标注。"""
    narrative = "考完最后一门，走出考场的时候腿是软的。"
    read = AsyncMock(return_value=_day_page(narrative))
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value=None),
        _no_arc(),
        _no_relationship_page(),
        patch("app.memory.context.read_latest_day_page", new=read),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _DAY_HEADER_MARK in out, "有昨天页时必须注入【你的昨天】段"
    assert narrative in out, "昨天页全文必须原样进 inner_context"
    assert "这页写于" in out
    assert "2026-06-10T23:40:00+08:00" in out
    assert read.await_args.kwargs == {"lane": "prod", "persona_id": "chiwei"}, (
        "最近一页昨天按 (lane, persona) 跨日期取最新，lane 口径 prod 归一"
    )


@pytest.mark.asyncio
async def test_day_section_absent_when_no_page():
    """她还没有昨天可忆（冷启动）→ 整段缺席、不补占位。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _DAY_HEADER_MARK not in out
    assert "你的昨天" not in out, "无页时不许出现任何昨天占位文案"


@pytest.mark.asyncio
async def test_pages_sit_between_scene_and_life_snapshot():
    """位置断言：场景 → 你们的关系 → 你的昨天 → 人生快照（你在和谁聊→你们的
    关系→你此刻状态的脉络；现有三段一字不动、只在中间插两段）。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在做饭，心情轻松"),
        ),
        _no_arc(),
        _rel_read(_relationship_page("他与我的一页。")),
        _day_read(_day_page("昨天留下的几笔。")),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    scene_pos = out.index("浩南")
    rel_pos = out.index(_REL_HEADER_MARK)
    day_pos = out.index(_DAY_HEADER_MARK)
    life_pos = out.index("做饭")
    assert scene_pos < rel_pos, "关系页必须在场景段之后"
    assert rel_pos < day_pos, "脉络：先你们的关系、再你的昨天"
    assert day_pos < life_pos, "两页必须在人生快照之前"


@pytest.mark.asyncio
async def test_arc_still_leads_when_pages_present():
    """加了两页后世界阶段仍是稳定前缀首段（现有段次序不被打乱）。"""
    arc_narrative = "一家人刚搬过来，眼下是初夏。"
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在做饭，心情轻松"),
        ),
        _arc_read(arc_narrative),
        _rel_read(_relationship_page("他与我的一页。")),
        _day_read(_day_page("昨天留下的几笔。")),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert out.index(_ARC_HEADER_MARK) < out.index("浩南") < out.index(_REL_HEADER_MARK)


@pytest.mark.asyncio
async def test_page_frames_have_no_hardcoded_plot_facts():
    """两段框架文案零剧情事实（高考 / 日期数字 / 角色名）——宪法，同 arc 透传。

    用无数字的占位 narrative / written_at 渲染后剥掉数据，剩下的就是纯框架文案。
    """
    rel_sentinel = "占位的关系页内容"
    day_sentinel = "占位的昨天页内容"
    written_sentinel = "占位的写页时刻"
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="生活快照占位"),
        ),
        _no_arc(),
        _rel_read(_relationship_page(rel_sentinel, written_at=written_sentinel)),
        _day_read(_day_page(day_sentinel, written_at=written_sentinel)),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="某人", persona_id="chiwei",
        )

    frame = (
        out.replace(rel_sentinel, "")
        .replace(day_sentinel, "")
        .replace(written_sentinel, "")
        .replace("某人", "")
        .replace("生活快照占位", "")
    )
    assert "高考" not in frame, "框架文案不得硬编剧情事实（高考）"
    assert not any(ch.isdigit() for ch in frame), "框架文案不得硬编日期 / 数字事实"
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert name not in frame, f"框架文案不得硬编角色名 {name!r}"


@pytest.mark.asyncio
async def test_page_read_errors_do_not_collapse_inner_context():
    """两页读失败 → 整段缺席但 inner_context 不塌（上下文增强不能塌掉 chat）。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        patch(
            "app.memory.context.read_relationship_page",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch(
            "app.memory.context.read_latest_day_page",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _REL_HEADER_MARK not in out
    assert _DAY_HEADER_MARK not in out
    assert "浩南" in out  # scene 照常
    assert "散步" in out  # life 快照照常
