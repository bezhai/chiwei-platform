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

from app.memory.context import (
    _build_life_state,
    _scene_section,
    build_inner_context,
)

CST = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _notebook_empty_by_default():
    """Default the notebook read to an empty book for every build_inner_context
    test so they stay DB-free; tests that need a non-empty book / to assert the
    read args patch ``list_notebook_entries`` explicitly inside their own block
    (an inner patch wins over this autouse default)."""
    with patch(
        "app.memory.context.list_notebook_entries",
        new=AsyncMock(return_value=[]),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_current_book_by_default():
    """Default the in-reading-book read to "no current book" (None) for every
    build_inner_context test so they stay DB-free and the reading-impression
    section never spuriously appears; tests that need a current book patch
    ``find_current_book_impression`` inside their own block (an inner patch wins
    over this autouse default). 书名由印象自带（book_title），注入不再 find_book_meta。"""
    with patch(
        "app.memory.context.find_current_book_impression",
        new=AsyncMock(return_value=None),
    ):
        yield


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
    """Stub the day-page read to None so unit tests stay DB-free."""
    return patch(
        "app.memory.context.read_day_page_before",
        new=AsyncMock(return_value=None),
    )


def _empty_notebook():
    """Stub the notebook read to an empty book so unit tests stay DB-free."""
    return patch(
        "app.memory.context.list_notebook_entries",
        new=AsyncMock(return_value=[]),
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
        "app.memory.context.read_day_page_before",
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
            chat_name="小群",
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
    """有最近一页昨天 → 注入【你的昨天】段：页全文 + written_at 时间标注。

    读口必须是 read_day_page_before（严格早于当前生活日，与 life 侧 #260 同
    口径）：清晨回笼觉的快班会给**当前生活日**写下凌晨短页，跨日取最新会把它
    错当「昨天」注进下午的聊天（2026-06-12 真实群聊 trace 实证过）。
    """
    from datetime import datetime

    from app.infra.cst_time import CST

    narrative = "考完最后一门，走出考场的时候腿是软的。"
    read = AsyncMock(return_value=_day_page(narrative))
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value=None),
        patch(
            "app.memory.context.now_cst",
            return_value=datetime(2026, 6, 12, 17, 0, tzinfo=CST),
        ),
        _no_arc(),
        _no_relationship_page(),
        patch("app.memory.context.read_day_page_before", new=read),
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
    assert read.await_args.kwargs == {
        "lane": "prod",
        "persona_id": "chiwei",
        "before_date": "2026-06-12",
    }, "昨天页只认日期严格早于当前生活日的最新一版，lane 口径 prod 归一"


@pytest.mark.asyncio
async def test_day_page_boundary_follows_living_day_not_calendar_day():
    """凌晨 02:00 聊天：当前生活日还是前一天（04:00 边界），before_date 必须
    跟生活日口径走——熬夜聊天时「昨天」不能被日历日切走。"""
    from datetime import datetime

    from app.infra.cst_time import CST

    read = AsyncMock(return_value=None)
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value=None),
        patch(
            "app.memory.context.now_cst",
            return_value=datetime(2026, 6, 13, 2, 0, tzinfo=CST),
        ),
        _no_arc(),
        _no_relationship_page(),
        patch("app.memory.context.read_day_page_before", new=read),
    ):
        await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert read.await_args.kwargs["before_date"] == "2026-06-12", (
        "06-13 凌晨 2 点仍属生活日 06-12，「昨天」的上界应是 06-12 而非 06-13"
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
            "app.memory.context.read_day_page_before",
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


# ---------------------------------------------------------------------------
# 备忘录 & 日程 第二块（进她脑子 · chat 侧）：inner_context 显式接上「她本子里还没
# 了结的事」一段。
#
# chat 概念上是 life 的快照，但工程上 inner_context 是显式拼几段——本子得**显式接进
# 去**才会出现在聊天里。同 life 唤醒侧：只读还活着（active_only=True）的条目，原样渲染
# （复用 render_notebook），绝不按年龄/条数/过期筛。无条目/读失败 → 整段缺席不补占位、
# inner_context 不塌。
# ---------------------------------------------------------------------------

_NOTEBOOK_HEADER_MARK = "【你本子里还没了结的事】"


def _notebook_entry(entry_id, content, *, remind_at=None, status="active"):
    from app.domain.notebook import NotebookEntry

    return NotebookEntry(
        lane="prod",
        persona_id="chiwei",
        entry_id=entry_id,
        content=content,
        remind_at=remind_at,
        status=status,
        noted_at="2026-06-13T10:00:00+08:00",
    )


def _notebook_read(entries):
    return patch(
        "app.memory.context.list_notebook_entries",
        new=AsyncMock(return_value=entries),
    )


@pytest.mark.asyncio
async def test_notebook_section_injected_when_active_entries_exist():
    """她本子里有还活着的条目 → inner_context 带「还没了结的事」段（含条目内容）。"""
    entries = [
        _notebook_entry("n1", "周末想陪我妹去琴行"),
        _notebook_entry("n2", "三点要去拿快递", remind_at="2026-06-13T15:00:00+08:00"),
    ]
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        _notebook_read(entries),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _NOTEBOOK_HEADER_MARK in out, "有还活着的条目时必须接上本子段"
    assert "周末想陪我妹去琴行" in out, "备忘内容必须进 inner_context（聊天能自然提到）"
    assert "三点要去拿快递" in out, "日程内容必须进 inner_context"


@pytest.mark.asyncio
async def test_notebook_read_with_active_only_true_correct_lane_persona():
    """接进 chat 的是 active_only=True、lane 与 persona 口径正确（不按年龄/条数/过期筛）。"""
    read = AsyncMock(return_value=[_notebook_entry("n1", "惦记的事")])
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value="ppe-x"),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        patch("app.memory.context.list_notebook_entries", new=read),
    ):
        await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert read.await_args.kwargs == {
        "lane": "ppe-x",
        "persona_id": "chiwei",
        "active_only": True,
    }, "chat 接进的只能是她没了结的（active_only=True），lane 走 current_deployment_lane()"


@pytest.mark.asyncio
async def test_notebook_lane_falls_back_to_prod():
    """LANE 未设（prod → None）归一到 'prod'，与本子写入端同口径。"""
    read = AsyncMock(return_value=[])
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value=None),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        patch("app.memory.context.list_notebook_entries", new=read),
    ):
        await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert read.await_args.kwargs == {
        "lane": "prod",
        "persona_id": "chiwei",
        "active_only": True,
    }


@pytest.mark.asyncio
async def test_empty_notebook_section_absent_no_placeholder():
    """空本子（没活着的条目）→ 整段缺席、绝不塞占位文案，inner_context 不塌。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        _notebook_read([]),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _NOTEBOOK_HEADER_MARK not in out
    assert "本子" not in out, "空本子时不许出现任何本子占位文案"
    assert "散步" in out  # life 快照照常


@pytest.mark.asyncio
async def test_notebook_read_failure_section_absent_context_still_builds():
    """本子读失败 → 整段缺席但 inner_context 不塌（上下文增强不能塌掉 chat）。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        patch(
            "app.memory.context.list_notebook_entries",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert _NOTEBOOK_HEADER_MARK not in out
    assert "浩南" in out  # scene 照常
    assert "散步" in out  # life 快照照常


# ---------------------------------------------------------------------------
# 读小说 Task 3（在读的书的印象进她脑子 · chat 侧）：inner_context 在 notebook 段
# 之后、人生快照之前接上「她正在读的那本书」一段。
#
# 她在读那本书的印象（find_current_book_impression 取最近读过一程、状态仍「在读」那
# 一本）每轮从 PG 重渲进她的 inner_context，让她聊天时书自然在心里。只渲染一本当前书
# （读完/放下的 find_current_book_impression 已排除）；无在读书 / 读失败 → 整段缺席不
# 补占位、inner_context 不塌（照 notebook 段的 fail-soft 姿势）。渲染复用
# render_reading_impression（单一定义处，与 life 唤醒侧同一份）。
# ---------------------------------------------------------------------------


def _book_impression(impression, *, book_title="挪威的森林", attachment_id="msg-1:fk1",
                     status="reading"):
    from app.domain.book_impression import BookImpression

    return BookImpression(
        lane="prod",
        persona_id="chiwei",
        attachment_id=attachment_id,
        book_title=book_title,
        impression=impression,
        pages_read=5,
        status=status,
        observed_at="2026-06-23T15:00:00+08:00",
    )


def _reading_read(impression):
    # 书名由印象自带（book_title）—— 注入不再 find_book_meta。
    return patch(
        "app.memory.context.find_current_book_impression",
        new=AsyncMock(return_value=impression),
    )


@pytest.mark.asyncio
async def test_reading_section_injected_when_reading_a_book():
    """她在读一本书 → inner_context 接上「在读的书」段（含书名 + 印象正文，书名印象自带）。"""
    imp = _book_impression("那个少年总让我想起小时候的自己。", book_title="挪威的森林")
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        _reading_read(imp),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert "挪威的森林" in out, "在读时书名（印象自带）必须进 inner_context"
    assert "那个少年总让我想起小时候的自己。" in out, "她的印象正文必须进 inner_context"


@pytest.mark.asyncio
async def test_reading_section_only_one_book():
    """注入只渲染一本当前书：find_current_book_impression 已保证只返回一本，inner_context 只含这本。"""
    imp = _book_impression("读到一半，停不下来。", book_title="百年孤独")
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        _reading_read(imp),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert out.count("百年孤独") == 1, "inner_context 里只出现这一本当前书"


@pytest.mark.asyncio
async def test_reading_section_no_find_book_meta():
    """书名从印象自带 book_title 渲、不查任何书注册表（注入去 find_book_meta，Task 3）。"""
    import app.memory.context as ctx

    assert not hasattr(ctx, "find_book_meta"), (
        "注入点不再 import / 依赖 find_book_meta（书注册表已删）"
    )


@pytest.mark.asyncio
async def test_reading_read_with_correct_lane_persona():
    """接进 chat 的在读印象读口 lane / persona 口径正确（lane 走 current_deployment_lane()）。"""
    cur = AsyncMock(return_value=None)
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        patch("app.memory.context.current_deployment_lane", return_value="ppe-x"),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        patch("app.memory.context.find_current_book_impression", new=cur),
    ):
        await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert cur.await_args.kwargs == {
        "lane": "ppe-x",
        "persona_id": "chiwei",
    }, "在读印象读口 lane 走 current_deployment_lane()、persona 正确"


@pytest.mark.asyncio
async def test_no_current_book_section_absent_no_placeholder():
    """无在读书（None）→ 整段缺席、绝不塞占位文案，inner_context 不塌。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        _reading_read(None),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert "在读" not in out, "无当前书时不许出现任何在读书占位文案"
    assert "散步" in out  # life 快照照常


@pytest.mark.asyncio
async def test_reading_read_failure_section_absent_context_still_builds():
    """在读印象读失败 → 整段缺席但 inner_context 不塌（上下文增强不能塌掉 chat）。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在散步"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
        patch(
            "app.memory.context.find_current_book_impression",
            new=AsyncMock(side_effect=RuntimeError("db down reading impression")),
        ),
    ):
        out = await build_inner_context(
            chat_id="oc_a", chat_type="p2p",
            user_ids=["u1"], trigger_user_id="u1",
            trigger_username="浩南", persona_id="chiwei",
        )

    assert "浩南" in out  # scene 照常
    assert "散步" in out  # life 快照照常


# ---------------------------------------------------------------------------
# task 5（chat 侧两正交维度）：scene 段把「通信介质」（飞书私聊 / 飞书群聊）和
# 「物理在场」分别标清，不压成一个「当前场合」标签（spec 决策 7：把它们压成一个
# 字段正是「把飞书当当面」的根）。chat 触发永远是隔着飞书打字、不是当面。
# ---------------------------------------------------------------------------

_MEDIUM_HEADER_MARK = "【通信介质】"
_PRESENCE_HEADER_MARK = "【物理在场】"


def test_scene_p2p_labels_medium_as_feishu_p2p():
    """p2p：通信介质标成「飞书私聊」+ 明确是隔着飞书打字（不是当面）。"""
    scene = _scene_section("p2p", "", "浩南")
    assert _MEDIUM_HEADER_MARK in scene, "scene 必须带【通信介质】维度标注"
    assert "飞书私聊" in scene, "p2p 通信介质要标成飞书私聊"
    assert "浩南" in scene, "要带对方名字"
    # 隔着飞书打字的措辞（与「当面」区分开，治混淆的根）
    assert "当面" not in scene or "不是当面" in scene


def test_scene_group_labels_medium_as_feishu_group():
    """群聊：通信介质标成「飞书群聊」+ 群名，仍带需回复谁的指示。"""
    scene = _scene_section("group", "高三家长群", "浩南")
    assert _MEDIUM_HEADER_MARK in scene, "scene 必须带【通信介质】维度标注"
    assert "飞书群聊" in scene, "group 通信介质要标成飞书群聊"
    assert "高三家长群" in scene, "要带群名"
    assert "浩南" in scene, "群聊要标明回复谁"


def test_scene_two_dimensions_not_collapsed_into_one_label():
    """两个维度各自成段：通信介质 + 物理在场分开标，不压成一个「当前场合」。

    spec 决策 7 命门：物理在场（她此刻在哪、跟谁面对面）和通信介质（隔着飞书打字）
    是两个正交维度，混成一个字段就是「把飞书当当面」的根。
    """
    scene = _scene_section("p2p", "", "浩南")
    assert _MEDIUM_HEADER_MARK in scene
    assert _PRESENCE_HEADER_MARK in scene
    # 两个标头分处不同位置（确实是两段、不是一个标签）
    assert scene.index(_MEDIUM_HEADER_MARK) != scene.index(_PRESENCE_HEADER_MARK)


def test_scene_presence_points_to_life_snapshot_not_chat_peer():
    """物理在场维度指向她的生活快照（她此刻在哪），不是把聊天对象当在身边。

    治混淆：聊天对象在飞书另一端、不在她身边。物理在场要她去看自己的生活状态，
    绝不暗示「浩南在你身边」。
    """
    scene = _scene_section("p2p", "", "浩南")
    presence_idx = scene.index(_PRESENCE_HEADER_MARK)
    presence_part = scene[presence_idx:]
    # 物理在场段不把聊天对象说成在她身边
    assert "浩南" not in presence_part, "物理在场段不该把聊天对象当成在她身边"


@pytest.mark.asyncio
async def test_inner_context_carries_both_dimensions_in_p2p():
    """端到端 p2p：inner_context 同时带【通信介质】(飞书私聊) 和【物理在场】两维度。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在房间里写作业"),
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

    assert _MEDIUM_HEADER_MARK in out
    assert "飞书私聊" in out
    assert _PRESENCE_HEADER_MARK in out
    # 物理在场维度由她的生活快照承载（她此刻在哪）
    assert "写作业" in out


@pytest.mark.asyncio
async def test_inner_context_carries_both_dimensions_in_group():
    """端到端 group：inner_context 带【通信介质】(飞书群聊 + 群名) 和【物理在场】。"""
    with (
        patch(
            "app.memory.context._build_life_state",
            new=AsyncMock(return_value="你此刻在客厅看电视"),
        ),
        _no_arc(),
        _no_relationship_page(),
        _no_day_page(),
    ):
        out = await build_inner_context(
            chat_id="oc_g", chat_type="group",
            user_ids=["u1", "u2"], trigger_user_id="u2",
            trigger_username="浩南", persona_id="chiwei",
            chat_name="高三家长群",
        )

    assert _MEDIUM_HEADER_MARK in out
    assert "飞书群聊" in out
    assert "高三家长群" in out
    assert _PRESENCE_HEADER_MARK in out
    assert "看电视" in out
