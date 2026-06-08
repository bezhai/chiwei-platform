"""DailyMaterials —— 每天一份外部底料简报落 durable PG（刀 3 Task2）.

通用抓取 agent 每天清晨用三个结构化查询 skill 自己去查、自己把今天的外部底料组织成
一段「今天的客观底料」中文话，落进 ``DailyMaterials``，给 world 当背景知识。表只存
agent 组织好的那段话：``briefing`` + ``date`` + ``lane`` + ``fetched_at``——每源是否
拿到 agent 已在 briefing 里说了，world 直接读那段话，不需要每源 *_text / *_ok。

Key=(lane, date)：每个泳道每天一份，``insert_idempotent`` 幂等（同一天重投只落一行）。

这些是真实 Postgres 持久化测试（testcontainers）+ 形态契约断言——新 durable Data
三步检查的第③步「端到端 insert+读回」就靠这里钉死：mock pg 等于什么都没测。lane
隔离是命门：coe / ppe 绝不能覆盖 prod 的底料。
"""

from __future__ import annotations

import pytest

from app.fetch.materials import (
    DailyMaterials,
    find_daily_materials,
    save_daily_materials,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def materials_db(test_db):
    await migrate(DailyMaterials, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# 形态契约（新 Data 三步检查第①②步：Key 带 lane、字段都是标量不撞 JSONB gap）
# ---------------------------------------------------------------------------


def test_daily_materials_key_carries_lane_and_date():
    """自然键 = (lane, date) —— lane 是泳道隔离硬约束，date 让每天唯一一份。"""
    from app.runtime.data import key_fields

    keys = set(key_fields(DailyMaterials))
    assert keys == {"lane", "date"}, f"Key 必须是 (lane, date)，实际 {keys}"


def test_daily_materials_has_no_dict_or_list_field():
    """所有字段都是标量（str）—— framework persist 层无 JSONB 编解码，放
    dict/list 会 asyncpg DataError。对齐 sibling WorldState / ThinkingTokensSpent。
    """
    for name, field in DailyMaterials.model_fields.items():
        ann = field.annotation
        assert ann not in (dict, list), (
            f"DailyMaterials.{name} 是 {ann}，framework 暂不能持久化结构化字段"
        )


def test_daily_materials_no_reserved_column_clash():
    """字段名不撞 framework 保留列（id / created_at / updated_at / dedup_hash）。

    时刻字段叫 ``fetched_at`` 而非 ``created_at`` / ``updated_at``——后者是 migrator
    自动加的保留列，业务字段绕开（对齐 ThinkingTokensSpent.observed_at 的口径）。
    """
    reserved = {"id", "created_at", "updated_at", "dedup_hash"}
    clash = reserved & set(DailyMaterials.model_fields)
    assert not clash, f"字段撞了 framework 保留列: {clash}"


def test_daily_materials_is_only_briefing_plus_keys_and_time():
    """表简化后只存 agent 组织好的那段底料话 + 自然键 + 抓取时刻。

    去掉了之前每源的 *_text / *_ok 字段——agent 已在 briefing 里说了哪项今天没拿到，
    world 直接读那段话，不需要机读每源状态。
    """
    fields = set(DailyMaterials.model_fields)
    assert fields == {"lane", "date", "briefing", "fetched_at"}, (
        f"DailyMaterials 应只有 lane/date/briefing/fetched_at，实际 {fields}"
    )
    # 旧的每源字段必须彻底消失（接口变更、不留坏字段）。
    for gone in (
        "weather_text",
        "anime_text",
        "holiday_text",
        "weather_ok",
        "anime_ok",
        "holiday_ok",
    ):
        assert gone not in fields, f"旧字段 {gone} 应已删除"


# ---------------------------------------------------------------------------
# 端到端 insert + 读回（三步检查第③步：真库证明能落能读）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_save_then_find_daily_materials(materials_db):
    """写一天的底料（只一段 briefing） → 按 (lane, date) 读回。"""
    await save_daily_materials(
        lane="coe-t3",
        date="2026-06-08",
        briefing="今天广州小雨，是个普通周末，番剧里 Re:Zero 第三季在更新。",
        fetched_at="2026-06-08T06:00:00+08:00",
    )

    mat = await find_daily_materials(lane="coe-t3", date="2026-06-08")
    assert mat is not None
    assert mat.date == "2026-06-08"
    assert mat.lane == "coe-t3"
    assert "Re:Zero" in mat.briefing
    assert "普通周末" in mat.briefing
    assert mat.fetched_at == "2026-06-08T06:00:00+08:00"


@pytest.mark.integration
async def test_daily_materials_lane_isolation(materials_db):
    """同 date 不同 lane 各一份，coe 绝不覆盖 prod 的底料。"""
    await save_daily_materials(
        lane="prod",
        date="2026-06-08",
        briefing="prod 简报",
        fetched_at="2026-06-08T06:00:00+08:00",
    )
    await save_daily_materials(
        lane="coe-t3",
        date="2026-06-08",
        briefing="coe 简报",
        fetched_at="2026-06-08T06:00:00+08:00",
    )

    prod = await find_daily_materials(lane="prod", date="2026-06-08")
    coe = await find_daily_materials(lane="coe-t3", date="2026-06-08")
    assert prod.briefing == "prod 简报"
    assert coe.briefing == "coe 简报"


@pytest.mark.integration
async def test_save_is_idempotent_same_day(materials_db):
    """同 (lane, date) 重投幂等：insert_idempotent ON CONFLICT DO NOTHING，第一次为准。"""
    await save_daily_materials(
        lane="coe-t3",
        date="2026-06-08",
        briefing="第一次简报",
        fetched_at="2026-06-08T06:00:00+08:00",
    )
    # 同一天再抓一次（重投）—— 幂等不应覆盖，也不应报错。
    await save_daily_materials(
        lane="coe-t3",
        date="2026-06-08",
        briefing="第二次简报（不该覆盖）",
        fetched_at="2026-06-08T07:00:00+08:00",
    )

    mat = await find_daily_materials(lane="coe-t3", date="2026-06-08")
    assert mat is not None
    assert mat.briefing == "第一次简报", "同一天重投幂等：第一次为准、不覆盖"


@pytest.mark.integration
async def test_find_cold_returns_none(materials_db):
    """没抓过的 (lane, date) 读回 None（world 据此知道今天还没底料）。"""
    assert await find_daily_materials(lane="coe-t3", date="2026-06-08") is None
