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
    advance_act_cursor,
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


# ---------------------------------------------------------------------------
# act 消费游标 —— world 按复合游标 (created_at, act_id) pull act 后推进游标
#
# 游标用 created_at（单调落库序）而非 occurred_at（life 轮首固定的做事时刻、与落库
# 顺序可乱序）—— out-of-order 漏读命门见 tests/domain/test_act_read.py。
# ---------------------------------------------------------------------------


def test_worldstate_has_nullable_act_cursor():
    """WorldState 多一对 act 消费游标字段，nullable（冷启动时为 None）。"""
    assert "act_cursor_created_at" in WorldState.model_fields
    assert "act_cursor_act_id" in WorldState.model_fields
    snap = WorldState(lane="x", world_time="t", detail="d")
    assert snap.act_cursor_created_at is None
    assert snap.act_cursor_act_id is None


def test_worldstate_has_no_occurred_at_cursor():
    """游标不再有 occurred_at 字段（已改成 created_at，零残留）。"""
    assert "act_cursor_occurred_at" not in WorldState.model_fields


@pytest.mark.integration
async def test_act_cursor_defaults_null_and_insert_roundtrips(world_db):
    """write_world_state 不带游标 → 列存 NULL（additive nullable 列能 insert+读回）。"""
    await write_world_state(
        lane="coe-t2",
        world_time="2026-06-05T20:00:00+08:00",
        detail="入夜。",
    )
    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.act_cursor_created_at is None
    assert snap.act_cursor_act_id is None


@pytest.mark.integration
async def test_advance_act_cursor_appends_version_writing_cursor(world_db):
    """advance_act_cursor append 一版、写进复合游标、保留最新 detail/world_time。"""
    await write_world_state(
        lane="coe-t2",
        world_time="2026-06-05T22:30:00+08:00",
        detail="夜深。",
    )
    await advance_act_cursor(
        lane="coe-t2",
        created_at="2026-06-05T22:40:00+08:00",
        act_id="a9",
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.act_cursor_created_at == "2026-06-05T22:40:00+08:00"
    assert snap.act_cursor_act_id == "a9"
    # detail / world_time 沿用上一版（advance_act_cursor 不丢叙述）
    assert snap.detail == "夜深。"
    assert snap.world_time == "2026-06-05T22:30:00+08:00"


@pytest.mark.integration
async def test_advance_act_cursor_preserves_next_wake_at(world_db):
    """advance_act_cursor 保留 next_wake_at（游标与到点时刻是两块独立调度状态）。"""
    await write_world_state(
        lane="coe-t2", world_time="t", detail="d"
    )
    await set_next_wake_at(lane="coe-t2", next_wake_at="2026-06-06T06:30:00+08:00")
    await advance_act_cursor(
        lane="coe-t2", created_at="2026-06-06T05:00:00+08:00", act_id="a1"
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap.next_wake_at == "2026-06-06T06:30:00+08:00", (
        "advance_act_cursor 不该丢掉 next_wake_at"
    )
    assert snap.act_cursor_act_id == "a1"


@pytest.mark.integration
async def test_set_next_wake_at_preserves_act_cursor(world_db):
    """set_next_wake_at 保留 act 游标（写到点时刻不丢已消费游标）。"""
    await write_world_state(lane="coe-t2", world_time="t", detail="d")
    await advance_act_cursor(
        lane="coe-t2", created_at="2026-06-06T05:00:00+08:00", act_id="a1"
    )
    await set_next_wake_at(lane="coe-t2", next_wake_at="2026-06-06T06:30:00+08:00")

    snap = await read_world_state(lane="coe-t2")
    assert snap.act_cursor_created_at == "2026-06-06T05:00:00+08:00"
    assert snap.act_cursor_act_id == "a1"
    assert snap.next_wake_at == "2026-06-06T06:30:00+08:00"


@pytest.mark.integration
async def test_write_world_state_preserves_scheduling_state(world_db):
    """update_world 改叙述时保留 next_wake_at + act 游标（叙述更新不清调度状态）。

    update_world 工具在循环中途调，只该更新世界叙述（world_time + detail），绝不
    清掉收口才写的 next_wake_at / 游标——否则每轮更新叙述都把游标打回 None、下轮
    重读全部 act。
    """
    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    await advance_act_cursor(
        lane="coe-t2", created_at="2026-06-06T05:00:00+08:00", act_id="a1"
    )
    await set_next_wake_at(lane="coe-t2", next_wake_at="2026-06-06T06:30:00+08:00")

    # 再调 update_world 改叙述（模拟下一轮中途）
    await write_world_state(lane="coe-t2", world_time="t1", detail="新叙述")

    snap = await read_world_state(lane="coe-t2")
    assert snap.detail == "新叙述"
    assert snap.world_time == "t1"
    assert snap.next_wake_at == "2026-06-06T06:30:00+08:00", (
        "update_world 不该清掉 next_wake_at"
    )
    assert snap.act_cursor_act_id == "a1", "update_world 不该清掉 act 游标"


@pytest.mark.integration
async def test_advance_act_cursor_cold_start_no_snapshot_is_safe(world_db):
    """从没写过 WorldState 的 lane 调 advance_act_cursor 不抛（冷启容错）。"""
    await advance_act_cursor(
        lane="coe-never", created_at="2026-06-06T05:00:00+08:00", act_id="a1"
    )
