"""客观世界叙述快照的持久化契约 — 阶段 1A（world 推演者）.

新范式下 world 只剩一块客观状态：

  * :class:`WorldState` —— 这家的客观世界叙述快照（世界时间 + 一段自然语言
    ``detail`` 叙述：谁大概在哪、在干嘛、什么氛围，位置融在叙述里），as_latest，
    Key 带 lane。每次 world 推演完 append 一版。

没有 presence 表了——"谁在哪"不再是结构化查表，而是融进 ``detail`` 自然语言
叙述里，由 world 推演维护。

这些都是真实 Postgres 持久化测试（testcontainers）——快照的正确性故事
全在"能不能 insert 进去、能不能按 lane 查回最新一版"，mock pg
等于什么都没测。lane 隔离是命门：coe / ppe 绝不能覆盖 prod 的"她此刻
客观世界"。
"""

from __future__ import annotations

import pytest

from app.world.state import (
    WorldState,
    read_world_state,
    set_next_wake_at,
    write_world_state,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def world_db(test_db):
    await migrate(WorldState, test_db)
    yield test_db


def test_worldstate_key_carries_lane():
    """WorldState 的自然键必须含 lane —— 泳道隔离的硬约束。"""
    from app.runtime.data import key_fields

    assert "lane" in key_fields(WorldState)


def test_worldstate_has_no_dict_or_list_field():
    """WorldState 不能有 dict / list 字段 —— framework persist 层无 JSONB
    编解码，放结构化字段会 asyncpg DataError。叙述都是 TEXT。
    """
    for name, field in WorldState.model_fields.items():
        ann = field.annotation
        assert ann not in (dict, list), (
            f"WorldState.{name} 是 {ann}，framework 暂不能持久化结构化字段"
        )


def test_worldstate_has_detail_field_not_situation():
    """WorldState 用 ``detail``（一段自然语言世界叙述），不再有 ``situation``。"""
    assert "detail" in WorldState.model_fields
    assert "situation" not in WorldState.model_fields


@pytest.mark.integration
async def test_write_then_read_world_state(world_db):
    """写一版 WorldState → 读回最新（含世界时间 + 客观叙述 detail）。"""
    await write_world_state(
        lane="coe-t2",
        world_time="2026-06-03T05:50:00+08:00",
        detail="天还没亮，三姐妹都在各自房间睡着，屋里很安静。",
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.world_time == "2026-06-03T05:50:00+08:00"
    assert "睡" in snap.detail


@pytest.mark.integration
async def test_world_state_lane_isolation(world_db):
    """同 lane 各一版，coe 绝不覆盖 prod 的客观世界快照。"""
    await write_world_state(
        lane="prod", world_time="2026-06-03T12:00:00+08:00", detail="prod 的中午"
    )
    await write_world_state(
        lane="coe-t2", world_time="2026-06-03T05:50:00+08:00", detail="coe 的清晨"
    )

    prod_snap = await read_world_state(lane="prod")
    coe_snap = await read_world_state(lane="coe-t2")
    assert prod_snap.detail == "prod 的中午"
    assert coe_snap.detail == "coe 的清晨"


@pytest.mark.integration
async def test_world_state_appends_new_version(world_db):
    """as_latest：再写一版 read 拿到的是最新那版（append-only 历史保留）。"""
    await write_world_state(
        lane="coe-t2", world_time="2026-06-03T05:50:00+08:00", detail="清晨"
    )
    await write_world_state(
        lane="coe-t2", world_time="2026-06-03T06:10:00+08:00", detail="千凪起床了，厨房有了动静"
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap.world_time == "2026-06-03T06:10:00+08:00"
    assert snap.detail == "千凪起床了，厨房有了动静"


@pytest.mark.integration
async def test_read_world_state_cold_start_returns_none(world_db):
    """没写过任何快照的 lane 读回 None（冷启动）。"""
    assert await read_world_state(lane="coe-never-written") is None


# ---------------------------------------------------------------------------
# next_wake_at —— 阶段 1B Task 1（到点 gate 的 state 字段）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_next_wake_at_defaults_null_and_insert_roundtrips(world_db):
    """write_world_state 不带 next_wake_at → 列存 NULL（additive nullable 列能 insert+读回）。"""
    await write_world_state(
        lane="coe-t2",
        world_time="2026-06-05T20:00:00+08:00",
        detail="入夜，三姐妹各自关门。",
    )
    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.next_wake_at is None, "没排过下次醒时 next_wake_at 为 None"


@pytest.mark.integration
async def test_set_next_wake_at_appends_version_preserving_detail(world_db):
    """set_next_wake_at append 一版、写进目标时刻、保留最新 detail/world_time。"""
    await write_world_state(
        lane="coe-t2",
        world_time="2026-06-05T22:30:00+08:00",
        detail="夜深了，屋里只剩冰箱的低鸣。",
    )
    target = "2026-06-06T06:30:00+08:00"
    await set_next_wake_at(lane="coe-t2", next_wake_at=target)

    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.next_wake_at == target, "next_wake_at 写入后能读回"
    # detail / world_time 沿用上一版（set_next_wake_at 不丢叙述）
    assert snap.detail == "夜深了，屋里只剩冰箱的低鸣。"
    assert snap.world_time == "2026-06-05T22:30:00+08:00"


@pytest.mark.integration
async def test_set_next_wake_at_cold_start_no_snapshot_is_noop_or_safe(world_db):
    """从没写过 WorldState 的 lane 调 set_next_wake_at 不抛（冷启容错）。"""
    # 不应抛异常；冷启时还没有可承载 next_wake_at 的快照，安全跳过即可。
    await set_next_wake_at(lane="coe-never", next_wake_at="2026-06-06T06:30:00+08:00")
