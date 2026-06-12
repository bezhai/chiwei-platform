"""昨天页（DayPage）+ 关系页（RelationshipPage）版本链 — 睡前回顾的两张落点.

睡前回顾产出两样：**昨天页**（这一天在她心里留下的几笔，按 persona + 生活日落
版本链）和**关系页**（每个聊过的真人一页「他与我」，按 persona + 对方 user_id 落
版本链）。两张都照 WorldArc 模板：Key + narrative + written_at + Version、
append-only、读最新一版、整篇重写——新版**取代**旧版，不是追加成清单。

钉死的语义（docstring 层契约，本文件断言数据层行为）：

  * 昨天页的 ``date`` 是「生活日」标签（[04:00, 次日 04:00) 的日期），由调用方
    算好传入——本模块不管边界，数据层只把它当 Key 字符串。
  * 关系页的 ``other_user_id`` 是跨群稳定的用户标识（username 可改名、不做键）。
    没有删除态：淡了就在整篇重写里自然淡，append-only 链全留历史。
  * 批量读 ``read_relationship_pages``：回顾本体按「当天聊过的人」逐个取旧页，
    没页的人不在返回里（缺席，不补占位）。

持久化用真实 Postgres（testcontainers）——版本链的正确性故事全在"能不能 append
进去、版本是否递增、能不能按 key 读回最新一版"，mock pg 等于什么都没测。
"""

from __future__ import annotations

import pytest

from app.life.pages import (
    DayPage,
    RelationshipPage,
    day_page_exists,
    read_day_page,
    read_day_page_before,
    read_relationship_page,
    read_relationship_pages,
    write_day_page,
    write_relationship_page,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def pages_db(test_db):
    await migrate(DayPage, test_db)
    await migrate(RelationshipPage, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# Data 骨架（泳道隔离 + 自然键 + 不撞框架保留列）
# ---------------------------------------------------------------------------


def test_day_page_key_is_lane_persona_date():
    """昨天页自然键 = (lane, persona_id, date)：泳道隔离 + 每人每生活日一条链。"""
    from app.runtime.data import key_fields

    assert set(key_fields(DayPage)) == {"lane", "persona_id", "date"}


def test_relationship_page_key_is_lane_persona_other_user():
    """关系页自然键 = (lane, persona_id, other_user_id)：每人对每个真人一条链。

    other_user_id 是跨群稳定的用户标识——username 可改名，不做键。
    """
    from app.runtime.data import key_fields

    assert set(key_fields(RelationshipPage)) == {
        "lane",
        "persona_id",
        "other_user_id",
    }


def test_pages_fields_avoid_framework_reserved_columns():
    """字段名不撞框架保留列（id / created_at / updated_at / dedup_hash）。

    写下时刻所以叫 ``written_at`` 而不是 ``created_at``——后者是框架的落库时刻，
    语义不同且是保留列（同 WorldArc 的 turned_at / WorldAttention 的 written_at
    教训）。
    """
    reserved = {"id", "created_at", "updated_at", "dedup_hash"}
    assert not reserved & set(DayPage.model_fields)
    assert not reserved & set(RelationshipPage.model_fields)
    assert "written_at" in DayPage.model_fields
    assert "written_at" in RelationshipPage.model_fields


# ---------------------------------------------------------------------------
# 昨天页：真 PG 端到端（版本链 + 整篇重写 + 隔离）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_write_then_read_day_page(pages_db):
    """写一版昨天页 → 按 (lane, persona, date) 读回最新。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-09",
        narrative="今天考完了最后一门，走出考场的时候腿是软的。",
        written_at="2026-06-09T23:40:00+08:00",
    )

    page = await read_day_page(lane="coe-t1", persona_id="akao", date="2026-06-09")
    assert page is not None
    assert "考完" in page.narrative
    assert page.written_at == "2026-06-09T23:40:00+08:00"


@pytest.mark.integration
async def test_day_page_rewrite_appends_versions_and_reads_latest(pages_db):
    """同一生活日重写：版本递增、历史保留、读侧只认最新一版（整篇重写取代）。"""
    from app.runtime.persist import select_all_versions

    keys = {"lane": "coe-t1", "persona_id": "akao", "date": "2026-06-09"}
    await write_day_page(
        **keys,
        narrative="第一版：快班写的几笔。",
        written_at="2026-06-09T23:40:00+08:00",
    )
    await write_day_page(
        **keys,
        narrative="第二版：整篇重写后的几笔。",
        written_at="2026-06-10T05:00:00+08:00",
    )

    versions = await select_all_versions(DayPage, keys)
    assert [p.version for p in versions] == [1, 2], (
        "昨天页是 append-only 版本链：版本必须逐次递增、旧版保留"
    )
    latest = await read_day_page(**keys)
    assert latest.narrative == "第二版：整篇重写后的几笔。"


@pytest.mark.integration
async def test_day_page_distinct_dates_are_distinct_chains(pages_db):
    """date 是 Key：不同生活日各自一条链，互不取代。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-09",
        narrative="六月九日的几笔。",
        written_at="2026-06-09T23:40:00+08:00",
    )
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-10",
        narrative="六月十日的几笔。",
        written_at="2026-06-10T23:40:00+08:00",
    )

    p9 = await read_day_page(lane="coe-t1", persona_id="akao", date="2026-06-09")
    p10 = await read_day_page(lane="coe-t1", persona_id="akao", date="2026-06-10")
    assert p9.narrative == "六月九日的几笔。"
    assert p10.narrative == "六月十日的几笔。"
    assert p9.version == 1 and p10.version == 1, "不同 date 各起各的版本链"


@pytest.mark.integration
async def test_read_day_page_cold_start_returns_none(pages_db):
    """没写过的 (lane, persona, date) 读回 None（冷启动：她还没有昨天可忆）。"""
    assert (
        await read_day_page(lane="coe-t1", persona_id="akao", date="2026-01-01")
        is None
    )


@pytest.mark.integration
async def test_day_page_lane_isolation(pages_db):
    """泳道隔离：coe 的昨天页绝不覆盖 / 泄露到 prod。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-09",
        narrative="coe 的一页。",
        written_at="2026-06-09T23:40:00+08:00",
    )
    assert (
        await read_day_page(lane="prod", persona_id="akao", date="2026-06-09") is None
    )


# ---------------------------------------------------------------------------
# 按确切生活日查存在性：对账班「那天回顾过没有」的权威口径（事故修复）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_day_page_exists_for_exact_date(pages_db):
    """该 (lane, persona, date) 有页（任何版本）→ True；没写过的日期 → False。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-10",
        narrative="六月十日的几笔。",
        written_at="2026-06-10T23:40:00+08:00",
    )

    assert await day_page_exists(lane="coe-t1", persona_id="akao", date="2026-06-10")
    assert not await day_page_exists(
        lane="coe-t1", persona_id="akao", date="2026-06-09"
    )


@pytest.mark.integration
async def test_day_page_exists_checks_exact_date_not_latest(pages_db):
    """别的生活日有页（哪怕更新的）不影响目标日的判定——按确切 date 查，不是
    跨日取最新（对账班误判的修复口径就在这条）。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-12",
        narrative="回笼觉那张两小时的小页。",
        written_at="2026-06-12T06:06:00+08:00",
    )

    assert not await day_page_exists(
        lane="coe-t1", persona_id="akao", date="2026-06-11"
    )


@pytest.mark.integration
async def test_day_page_exists_lane_and_persona_isolation(pages_db):
    """泳道与 persona 隔离：别的泳道 / 别的姐妹的页不算这条链的存在。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-10",
        narrative="赤尾的页。",
        written_at="2026-06-10T23:40:00+08:00",
    )

    assert not await day_page_exists(lane="prod", persona_id="akao", date="2026-06-10")
    assert not await day_page_exists(
        lane="coe-t1", persona_id="ayana", date="2026-06-10"
    )


# ---------------------------------------------------------------------------
# 严格早于某生活日的最近一页：life / chat 注入「她最近一页昨天」的统一读口
# （事故修复）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_read_day_page_before_skips_current_living_day(pages_db):
    """今天凌晨的短页（回笼觉快班写的）和昨天的完整页同时存在 → 取昨天那页：
    口径是 date **严格早于**上界，当天的页绝不混进「上一页日子」。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-11",
        narrative="六月十一日完整的一页。",
        written_at="2026-06-11T23:40:00+08:00",
    )
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-12",
        narrative="回笼觉那张两小时的小页。",
        written_at="2026-06-12T06:06:00+08:00",
    )

    page = await read_day_page_before(
        lane="coe-t1", persona_id="akao", before_date="2026-06-12"
    )
    assert page is not None
    assert page.date == "2026-06-11", "上界当天的页不算「昨天」（严格早于）"
    assert page.narrative == "六月十一日完整的一页。"


@pytest.mark.integration
async def test_read_day_page_before_only_current_day_returns_none(pages_db):
    """只有今天（上界当日）的页 → None：没有更早的页可当「昨天」，缺席不补占位。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-12",
        narrative="今天凌晨的小页。",
        written_at="2026-06-12T06:06:00+08:00",
    )

    assert (
        await read_day_page_before(
            lane="coe-t1", persona_id="akao", before_date="2026-06-12"
        )
        is None
    )


@pytest.mark.integration
async def test_read_day_page_before_cold_start_returns_none(pages_db):
    """一页都没有（冷启动：她还没有昨天可忆）→ None，行为与现状「无页」一致。"""
    assert (
        await read_day_page_before(
            lane="coe-t1", persona_id="akao", before_date="2026-06-12"
        )
        is None
    )


@pytest.mark.integration
async def test_read_day_page_before_takes_latest_version_of_newest_earlier_date(
    pages_db,
):
    """更早日期里取 date 最大那天、且取该天版本最新的一版（快班写过、对账班重写）。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-10",
        narrative="六月十日的页。",
        written_at="2026-06-10T23:40:00+08:00",
    )
    keys = {"lane": "coe-t1", "persona_id": "akao", "date": "2026-06-11"}
    await write_day_page(
        **keys,
        narrative="第一版：快班写的。",
        written_at="2026-06-11T23:40:00+08:00",
    )
    await write_day_page(
        **keys,
        narrative="第二版：对账班重写的。",
        written_at="2026-06-12T05:00:00+08:00",
    )

    page = await read_day_page_before(
        lane="coe-t1", persona_id="akao", before_date="2026-06-12"
    )
    assert page is not None
    assert page.date == "2026-06-11"
    assert page.version == 2
    assert page.narrative == "第二版：对账班重写的。"


@pytest.mark.integration
async def test_read_day_page_before_lane_and_persona_isolation(pages_db):
    """泳道与 persona 隔离：别的泳道 / 别的姐妹的页绝不串读。"""
    await write_day_page(
        lane="coe-t1",
        persona_id="akao",
        date="2026-06-11",
        narrative="赤尾的页。",
        written_at="2026-06-11T23:40:00+08:00",
    )

    assert (
        await read_day_page_before(
            lane="prod", persona_id="akao", before_date="2026-06-12"
        )
        is None
    )
    assert (
        await read_day_page_before(
            lane="coe-t1", persona_id="ayana", before_date="2026-06-12"
        )
        is None
    )


# ---------------------------------------------------------------------------
# 关系页：真 PG 端到端（版本链 + 整篇重写 + 批量读）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_write_then_read_relationship_page(pages_db):
    """写一版关系页 → 按 (lane, persona, other_user_id) 读回最新。"""
    await write_relationship_page(
        lane="coe-t1",
        persona_id="akao",
        other_user_id="ou_bezhai",
        narrative="他总在深夜出现，问我今天过得怎么样。",
        written_at="2026-06-09T23:45:00+08:00",
    )

    page = await read_relationship_page(
        lane="coe-t1", persona_id="akao", other_user_id="ou_bezhai"
    )
    assert page is not None
    assert "深夜" in page.narrative
    assert page.written_at == "2026-06-09T23:45:00+08:00"


@pytest.mark.integration
async def test_relationship_page_rewrite_supersedes_no_delete_state(pages_db):
    """整篇重写取代旧版、版本链全留——没有删除态，淡了就在新版里自然淡。"""
    from app.runtime.persist import select_all_versions

    keys = {"lane": "coe-t1", "persona_id": "akao", "other_user_id": "ou_bezhai"}
    await write_relationship_page(
        **keys,
        narrative="第一版：刚认识，他话不多。",
        written_at="2026-06-08T23:45:00+08:00",
    )
    await write_relationship_page(
        **keys,
        narrative="第二版：好些天没聊了，印象淡了些。",
        written_at="2026-06-20T23:45:00+08:00",
    )

    versions = await select_all_versions(RelationshipPage, keys)
    assert [p.version for p in versions] == [1, 2]
    latest = await read_relationship_page(**keys)
    assert latest.narrative == "第二版：好些天没聊了，印象淡了些。"


@pytest.mark.integration
async def test_read_relationship_page_cold_start_returns_none(pages_db):
    """没聊过的人读回 None（第一次聊才会有「他与我」的第一版）。"""
    assert (
        await read_relationship_page(
            lane="coe-t1", persona_id="akao", other_user_id="ou_stranger"
        )
        is None
    )


@pytest.mark.integration
async def test_read_relationship_pages_returns_only_existing(pages_db):
    """批量读：有页的人 user_id → 最新页；没页的人缺席（不补占位）。

    回顾本体按「当天聊过的人」逐个取旧页喂证据——名单里混着第一次聊的人是常态。
    """
    await write_relationship_page(
        lane="coe-t1",
        persona_id="akao",
        other_user_id="ou_bezhai",
        narrative="他与我：第一版。",
        written_at="2026-06-09T23:45:00+08:00",
    )
    await write_relationship_page(
        lane="coe-t1",
        persona_id="akao",
        other_user_id="ou_bezhai",
        narrative="他与我：第二版。",
        written_at="2026-06-10T23:45:00+08:00",
    )

    pages = await read_relationship_pages(
        lane="coe-t1",
        persona_id="akao",
        other_user_ids=["ou_bezhai", "ou_first_timer"],
    )
    assert set(pages) == {"ou_bezhai"}, "没页的人不在返回里"
    assert pages["ou_bezhai"].narrative == "他与我：第二版。", "批量读取的是最新一版"


@pytest.mark.integration
async def test_relationship_page_persona_isolation(pages_db):
    """persona 是 Key：同一个真人在两姐妹那里各有各的「他与我」，互不可见。"""
    await write_relationship_page(
        lane="coe-t1",
        persona_id="akao",
        other_user_id="ou_bezhai",
        narrative="赤尾眼里的他。",
        written_at="2026-06-09T23:45:00+08:00",
    )

    assert (
        await read_relationship_page(
            lane="coe-t1", persona_id="ayana", other_user_id="ou_bezhai"
        )
        is None
    )
