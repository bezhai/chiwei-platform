"""persona 版本链（PersonaVersion）— 「她是谁」的身份正文落点.

persona 慢漂（周级 review）不 UPDATE ``bot_persona`` 主表，而是落 framework Data
版本链：每版带来源（seed＝出厂灌入 / review＝自动慢漂 / owner＝bezhai 干预），
照 WorldArc / DayPage 模板：Key + narrative + written_at + Version、append-only、
读最新一版、整篇重写——新版**取代**旧版。

钉死的语义（docstring 层契约，本文件断言数据层行为）：

  * 读 a（读路径）：最新一版**不分来源**——owner 盖版即生效。
  * 读 b / 读 c（自动班的幂等与证据游标）：**只认 source='review'**——owner/seed
    版本既不挡当周自动班、也不推走证据窗口。
  * 周界 = 自然周一 00:00 CST（生活日是 04:00 界，但周界用自然周一零点，
    spec 决策 4）。
  * v0 灌入：链为空时把 ``bot_persona.persona_lite`` 原文落为第一版
    （source='seed'）；链非空零操作，重跑无害。

持久化用真实 Postgres（testcontainers）——版本链的正确性故事全在"能不能 append
进去、版本是否递增、来源过滤是否只认 review"，mock pg 等于什么都没测。
"""

from __future__ import annotations

from datetime import datetime

import pytest

import app.data.session as session_mod
from app.infra.cst_time import CST
from app.life.persona_chain import (
    PersonaVersion,
    has_review_version_this_week,
    read_latest_persona_version,
    read_latest_review_written_at,
    seed_persona_chain,
    week_start_cst,
    write_persona_version,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def chain_db(test_db):
    await migrate(PersonaVersion, test_db)
    yield test_db


@pytest.fixture
async def seed_db(chain_db):
    """补上 ``bot_persona`` 主表（SQLAlchemy 表）——v0 灌入要从它读原文。"""
    from app.data.models import Base, BotPersona

    async with chain_db.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[BotPersona.__table__])
        )
    yield chain_db


async def _seed_bot_persona(persona_id: str, persona_lite: str) -> None:
    from app.data.models import BotPersona

    async with session_mod.get_session() as s:
        s.add(
            BotPersona(
                persona_id=persona_id,
                display_name="赤尾",
                persona_core="（遗留字段，未被注入）",
                persona_lite=persona_lite,
                default_reply_style="自然",
                error_messages={},
                appearance_detail="红发",
            )
        )


# ---------------------------------------------------------------------------
# Data 骨架（泳道隔离 + 自然键 + 不撞框架保留列）
# ---------------------------------------------------------------------------


def test_persona_version_key_is_lane_persona():
    """版本链自然键 = (lane, persona_id)：泳道隔离 + 每个角色一条链。"""
    from app.runtime.data import key_fields

    assert set(key_fields(PersonaVersion)) == {"lane", "persona_id"}


def test_persona_version_fields_avoid_framework_reserved_columns():
    """字段名不撞框架保留列（id / created_at / updated_at / dedup_hash）。

    写下时刻叫 ``written_at`` 而不是 ``created_at``——后者是框架的落库时刻，
    语义不同且是保留列（同 DayPage / WorldAttention 教训）。
    """
    reserved = {"id", "created_at", "updated_at", "dedup_hash"}
    assert not reserved & set(PersonaVersion.model_fields)
    assert "written_at" in PersonaVersion.model_fields
    assert "source" in PersonaVersion.model_fields


# ---------------------------------------------------------------------------
# 周界：自然周一 00:00 CST（纯函数，先钉口径）
# ---------------------------------------------------------------------------


def test_week_start_is_monday_midnight_cst():
    """周三中午 → 本周一 00:00 CST。"""
    wednesday = datetime(2026, 6, 10, 12, 30, tzinfo=CST)
    assert week_start_cst(wednesday) == datetime(2026, 6, 8, 0, 0, tzinfo=CST)


def test_week_start_on_monday_just_after_midnight_is_same_day():
    """周一 00:01 → 当天 00:00（已进入新一周）。"""
    monday = datetime(2026, 6, 8, 0, 1, tzinfo=CST)
    assert week_start_cst(monday) == datetime(2026, 6, 8, 0, 0, tzinfo=CST)


def test_week_start_on_sunday_late_night_is_previous_monday():
    """周日 23:59 → 上一个周一 00:00（还在旧一周）。"""
    sunday = datetime(2026, 6, 7, 23, 59, tzinfo=CST)
    assert week_start_cst(sunday) == datetime(2026, 6, 1, 0, 0, tzinfo=CST)


def test_week_start_converts_other_timezones_to_cst_first():
    """跨时区先归一 CST 再取周界：UTC 周日 16:01 = CST 周一 00:01 → 新一周。"""
    from datetime import UTC

    sunday_utc = datetime(2026, 6, 7, 16, 1, tzinfo=UTC)
    assert week_start_cst(sunday_utc) == datetime(2026, 6, 8, 0, 0, tzinfo=CST)


# ---------------------------------------------------------------------------
# 读 a：真 PG 端到端（版本链 + owner 盖版即生效 + 隔离）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_write_then_read_latest_persona_version(chain_db):
    """写一版 → 按 (lane, persona_id) 读回最新（端到端 insert + 读回）。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="她是刚考完高考的赤尾。",
        source="seed",
        written_at="2026-06-08T10:00:00+08:00",
    )

    latest = await read_latest_persona_version(lane="coe-t1", persona_id="akao")
    assert latest is not None
    assert latest.narrative == "她是刚考完高考的赤尾。"
    assert latest.source == "seed"
    assert latest.written_at == "2026-06-08T10:00:00+08:00"
    assert latest.version == 1


@pytest.mark.integration
async def test_versions_append_and_owner_supersedes(chain_db):
    """seed → review → owner 三版递增；读 a 不分来源——owner 盖版即生效。"""
    from app.runtime.persist import select_all_versions

    keys = {"lane": "coe-t1", "persona_id": "akao"}
    await write_persona_version(
        **keys,
        narrative="第一版：出厂正文。",
        source="seed",
        written_at="2026-06-08T10:00:00+08:00",
    )
    await write_persona_version(
        **keys,
        narrative="第二版：review 慢漂后的正文。",
        source="review",
        written_at="2026-06-08T11:00:00+08:00",
    )
    await write_persona_version(
        **keys,
        narrative="第三版：bezhai 干预盖掉的正文。",
        source="owner",
        written_at="2026-06-08T12:00:00+08:00",
    )

    versions = await select_all_versions(PersonaVersion, keys)
    assert [v.version for v in versions] == [1, 2, 3], (
        "版本链 append-only：版本逐次递增、旧版保留"
    )
    latest = await read_latest_persona_version(**keys)
    assert latest.source == "owner"
    assert latest.narrative == "第三版：bezhai 干预盖掉的正文。"


@pytest.mark.integration
async def test_read_latest_cold_chain_returns_none(chain_db):
    """没写过的 (lane, persona_id) 读回 None（冷启：读侧 fallback 主表）。"""
    assert (
        await read_latest_persona_version(lane="coe-t1", persona_id="akao") is None
    )


@pytest.mark.integration
async def test_persona_chain_lane_and_persona_isolation(chain_db):
    """泳道与 persona 隔离：coe 的版本绝不泄露到 prod、姐妹之间互不可见。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="coe 里赤尾的一版。",
        source="seed",
        written_at="2026-06-08T10:00:00+08:00",
    )

    assert (
        await read_latest_persona_version(lane="prod", persona_id="akao") is None
    )
    assert (
        await read_latest_persona_version(lane="coe-t1", persona_id="ayana") is None
    )


# ---------------------------------------------------------------------------
# 读 b：本周是否已有 review 版本（只认 review——owner/seed 不挡班）
# ---------------------------------------------------------------------------

# 固定"现在"= 2026-06-09（周二）12:00 CST，本周一 = 2026-06-08 00:00 CST。
_NOW = datetime(2026, 6, 9, 12, 0, tzinfo=CST)


@pytest.mark.integration
async def test_review_this_week_true_when_review_written_in_week(chain_db):
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="本周慢漂的一版。",
        source="review",
        written_at="2026-06-08T05:00:00+08:00",
    )

    assert await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_review_this_week_ignores_seed_and_owner(chain_db):
    """同周 seed + owner 版本在场仍返回 False——只认 review，不挡自动班。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="本周灌入的出厂版。",
        source="seed",
        written_at="2026-06-08T05:00:00+08:00",
    )
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="本周 bezhai 盖的版。",
        source="owner",
        written_at="2026-06-09T09:00:00+08:00",
    )

    assert not await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_review_last_sunday_night_does_not_count_this_week(chain_db):
    """周日 23:59 写的 review 归上一周：周界是自然周一 00:00 CST。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="上周日深夜的一版。",
        source="review",
        written_at="2026-06-07T23:59:00+08:00",
    )

    assert not await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_review_monday_just_after_midnight_counts_this_week(chain_db):
    """周一 00:01 写的 review 归本周（与上一条合起来钉死周一边界）。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="周一凌晨的一版。",
        source="review",
        written_at="2026-06-08T00:01:00+08:00",
    )

    assert await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_review_this_week_cold_chain_is_false(chain_db):
    assert not await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


# ---------------------------------------------------------------------------
# 读 c：最新一条 review 版本的 written_at（证据游标，owner/seed 不动游标）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_latest_review_written_at_ignores_seed_and_owner(chain_db):
    """seed → review → owner 之后，游标 = review 那版的 written_at（不被 owner 推走）。"""
    keys = {"lane": "coe-t1", "persona_id": "akao"}
    await write_persona_version(
        **keys,
        narrative="出厂版。",
        source="seed",
        written_at="2026-06-01T10:00:00+08:00",
    )
    await write_persona_version(
        **keys,
        narrative="慢漂版。",
        source="review",
        written_at="2026-06-08T05:00:00+08:00",
    )
    await write_persona_version(
        **keys,
        narrative="bezhai 盖版。",
        source="owner",
        written_at="2026-06-09T09:00:00+08:00",
    )

    assert (
        await read_latest_review_written_at(**keys) == "2026-06-08T05:00:00+08:00"
    )


@pytest.mark.integration
async def test_latest_review_written_at_none_when_no_review(chain_db):
    """链上只有 seed / owner（或链为空）→ None：首跑窗口 = 全部现存页。"""
    assert (
        await read_latest_review_written_at(lane="coe-t1", persona_id="akao") is None
    )

    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="出厂版。",
        source="seed",
        written_at="2026-06-01T10:00:00+08:00",
    )
    assert (
        await read_latest_review_written_at(lane="coe-t1", persona_id="akao") is None
    )


# ---------------------------------------------------------------------------
# v0 灌入：链为空时把 bot_persona.persona_lite 落为第一版（source='seed'），幂等
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_seed_persona_chain_copies_persona_lite_verbatim(seed_db):
    """首跑：bot_persona.persona_lite 原文一字不差落为第一版 seed。"""
    await _seed_bot_persona("akao", "出厂身份正文：她是住在杭州的赤尾。")

    assert await seed_persona_chain(lane="coe-t1", persona_id="akao") is True

    latest = await read_latest_persona_version(lane="coe-t1", persona_id="akao")
    assert latest is not None
    assert latest.narrative == "出厂身份正文：她是住在杭州的赤尾。"
    assert latest.source == "seed"
    assert latest.version == 1


@pytest.mark.integration
async def test_seed_persona_chain_is_idempotent(seed_db):
    """连跑两次只有一行：链非空零操作，重跑无害。"""
    from app.runtime.persist import select_all_versions

    await _seed_bot_persona("akao", "出厂身份正文。")

    assert await seed_persona_chain(lane="coe-t1", persona_id="akao") is True
    assert await seed_persona_chain(lane="coe-t1", persona_id="akao") is False

    versions = await select_all_versions(
        PersonaVersion, {"lane": "coe-t1", "persona_id": "akao"}
    )
    assert len(versions) == 1


@pytest.mark.integration
async def test_seed_persona_chain_noop_when_chain_already_has_versions(seed_db):
    """链上已有任何版本（哪怕是 review/owner）→ 灌入零操作，不覆盖现状。"""
    await _seed_bot_persona("akao", "出厂身份正文。")
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="已有的 review 版。",
        source="review",
        written_at="2026-06-08T05:00:00+08:00",
    )

    assert await seed_persona_chain(lane="coe-t1", persona_id="akao") is False

    latest = await read_latest_persona_version(lane="coe-t1", persona_id="akao")
    assert latest.narrative == "已有的 review 版。"


@pytest.mark.integration
async def test_seed_persona_chain_missing_bot_persona_fails_fast(seed_db):
    """bot_persona 没有这行 → fail fast（没有原文可灌，不静默写空版）。"""
    with pytest.raises(ValueError):
        await seed_persona_chain(lane="coe-t1", persona_id="ghost")


@pytest.mark.integration
async def test_seed_version_does_not_satisfy_review_idempotency(seed_db):
    """灌入的 seed 版不算 review：读 b 仍 False、读 c 仍 None（自动班照常跑）。"""
    await _seed_bot_persona("akao", "出厂身份正文。")
    await seed_persona_chain(lane="coe-t1", persona_id="akao")

    assert not await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )
    assert (
        await read_latest_review_written_at(lane="coe-t1", persona_id="akao") is None
    )
