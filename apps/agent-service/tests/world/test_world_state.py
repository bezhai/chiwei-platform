"""客观世界快照的持久化契约 — Task 2 (world engine).

world 维护两块客观状态：

  * :class:`WorldState` —— 这家的客观世界快照（世界时间 + 客观情形），
    as_latest，Key 带 lane。每次 world 推演完 append 一版。
  * :class:`RoomPresence` —— 房间级在场，**拍平成每个 persona 一行**
    （as_latest per (lane, persona)）。拍平是因为 framework 的 persist 层
    暂不能把 dict 字段序列化进 JSONB（放 dict 会 asyncpg DataError），
    所以"谁在哪个房间"不塞一个 dict 字段，而是一 persona 一行的
    ``room_id``。

这些都是真实 Postgres 持久化测试（testcontainers）——快照的正确性故事
全在"能不能 insert 进去、能不能按 lane / persona 查回最新一版"，mock pg
等于什么都没测。lane 隔离是命门：coe / ppe 绝不能覆盖 prod 的"她此刻
客观世界"。
"""

from __future__ import annotations

import pytest

from app.world.state import (
    RoomPresence,
    WorldState,
    read_presence,
    read_world_state,
    set_presence,
    write_world_state,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def world_db(test_db):
    await migrate(WorldState, test_db)
    await migrate(RoomPresence, test_db)
    yield test_db


def test_worldstate_key_carries_lane():
    """WorldState 的自然键必须含 lane —— 泳道隔离的硬约束。"""
    from app.runtime.data import key_fields

    assert "lane" in key_fields(WorldState)


def test_worldstate_has_no_dict_or_list_field():
    """WorldState 不能有 dict / list 字段 —— framework persist 层无 JSONB
    编解码，放结构化字段会 asyncpg DataError。在场用拍平的 RoomPresence 表。
    """
    for name, field in WorldState.model_fields.items():
        ann = field.annotation
        assert ann not in (dict, list), (
            f"WorldState.{name} 是 {ann}，framework 暂不能持久化结构化字段"
        )


def test_roompresence_is_flattened_per_persona():
    """在场拍平：每个 persona 一行，Key=(lane, persona_id)，载房间 id。"""
    from app.runtime.data import key_fields

    keys = key_fields(RoomPresence)
    assert "lane" in keys
    assert "persona_id" in keys
    assert "room_id" in RoomPresence.model_fields


@pytest.mark.integration
async def test_write_then_read_world_state(world_db):
    """写一版 WorldState → 读回最新（含世界时间 + 客观情形）。"""
    await write_world_state(
        lane="coe-t2",
        world_time="2026-06-03T05:50:00+08:00",
        situation="天还没亮，四个人都在各自房间睡着。",
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.world_time == "2026-06-03T05:50:00+08:00"
    assert "睡" in snap.situation


@pytest.mark.integration
async def test_world_state_lane_isolation(world_db):
    """同 lane 各一版，coe 绝不覆盖 prod 的客观世界快照。"""
    await write_world_state(
        lane="prod", world_time="2026-06-03T12:00:00+08:00", situation="prod 的中午"
    )
    await write_world_state(
        lane="coe-t2", world_time="2026-06-03T05:50:00+08:00", situation="coe 的清晨"
    )

    prod_snap = await read_world_state(lane="prod")
    coe_snap = await read_world_state(lane="coe-t2")
    assert prod_snap.situation == "prod 的中午"
    assert coe_snap.situation == "coe 的清晨"


@pytest.mark.integration
async def test_world_state_appends_new_version(world_db):
    """as_latest：再写一版 read 拿到的是最新那版（append-only 历史保留）。"""
    await write_world_state(
        lane="coe-t2", world_time="2026-06-03T05:50:00+08:00", situation="清晨"
    )
    await write_world_state(
        lane="coe-t2", world_time="2026-06-03T06:10:00+08:00", situation="千凪起床了"
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap.world_time == "2026-06-03T06:10:00+08:00"
    assert snap.situation == "千凪起床了"


@pytest.mark.integration
async def test_set_and_read_presence(world_db):
    """设某 persona 在场 → 读回她在哪个房间。"""
    await set_presence(lane="coe-t2", persona_id="chinagi", room_id="kitchen")
    await set_presence(lane="coe-t2", persona_id="akao", room_id="akao_room")

    chinagi = await read_presence(lane="coe-t2", persona_id="chinagi")
    akao = await read_presence(lane="coe-t2", persona_id="akao")
    assert chinagi == "kitchen"
    assert akao == "akao_room"


@pytest.mark.integration
async def test_presence_updates_to_latest_room(world_db):
    """位置变更：as_latest 读到的是最新房间（千凪从房间走到厨房）。"""
    await set_presence(lane="coe-t2", persona_id="chinagi", room_id="chinagi_room")
    await set_presence(lane="coe-t2", persona_id="chinagi", room_id="kitchen")

    assert await read_presence(lane="coe-t2", persona_id="chinagi") == "kitchen"


@pytest.mark.integration
async def test_presence_lane_isolation(world_db):
    """同 persona 不同 lane 在场互不干扰。"""
    await set_presence(lane="prod", persona_id="akao", room_id="living_room")
    await set_presence(lane="coe-t2", persona_id="akao", room_id="akao_room")

    assert await read_presence(lane="prod", persona_id="akao") == "living_room"
    assert await read_presence(lane="coe-t2", persona_id="akao") == "akao_room"


@pytest.mark.integration
async def test_read_presence_absent_persona_returns_none(world_db):
    """没设过在场的 persona 读回 None（不在场）。"""
    assert await read_presence(lane="coe-t2", persona_id="ghost") is None
