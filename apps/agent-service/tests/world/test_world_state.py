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
    record_world_round_close,
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
    """WorldState 不放 dict / list 字段 —— 它的形态选择：客观世界叙述就是一段
    自然语言 detail（TEXT），"谁在哪、在干嘛"融在叙述里、不拆结构化表。
    （framework 已支持 dict/list→JSONB，这里是业务设计约束、不是能力限制。）
    """
    for name, field in WorldState.model_fields.items():
        ann = field.annotation
        assert ann not in (dict, list), (
            f"WorldState.{name} 是 {ann}，WorldState 设计上叙述用 TEXT、不用结构化字段"
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


# ---------------------------------------------------------------------------
# materials_ingested_date —— 刀 3 调整：world「当天第一次醒纳入底料一次」的收口标记
#
# world 当天第一次醒、有底料且未纳入时把底料拼进意识流，收口时把
# materials_ingested_date 标成今天；之后当天后续轮次不再重喂。这个标记必须和收口
# 的游标推进并进同一次 WorldState append（原子、幂等、不和游标 / 到点时刻打架）。
# ---------------------------------------------------------------------------


def test_worldstate_has_nullable_materials_ingested_date():
    """WorldState 多一个 materials_ingested_date 字段，nullable，默认 None（从没纳入过）。"""
    assert "materials_ingested_date" in WorldState.model_fields
    snap = WorldState(lane="x", world_time="t", detail="d")
    assert snap.materials_ingested_date is None


@pytest.mark.integration
async def test_materials_ingested_date_defaults_null_and_insert_roundtrips(world_db):
    """write_world_state 不带 materials_ingested_date → 列存 NULL（additive nullable 列）。"""
    await write_world_state(
        lane="coe-t2",
        world_time="2026-06-08T06:30:00+08:00",
        detail="清晨。",
    )
    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.materials_ingested_date is None, "从没纳入过底料时该字段为 None"


@pytest.mark.integration
async def test_record_world_round_close_marks_materials_and_advances_cursor(world_db):
    """收口：同一次 append 既推进游标到本批末尾、又标记底料已纳入今天（原子、一版）。"""
    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")

    await record_world_round_close(
        lane="coe-t2",
        advance_cursor_to=("2026-06-08T08:05:00+08:00", "a5"),
        materials_ingested_date="2026-06-08",
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.act_cursor_created_at == "2026-06-08T08:05:00+08:00"
    assert snap.act_cursor_act_id == "a5"
    assert snap.materials_ingested_date == "2026-06-08"
    # 叙述沿用上一版（收口不丢世界叙述）
    assert snap.detail == "d0"
    assert snap.world_time == "t0"


@pytest.mark.integration
async def test_record_world_round_close_marks_materials_on_empty_batch(world_db):
    """空批次（没新 act 但当天首醒纳入了底料）→ 不推游标、但标记底料已纳入。"""
    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    await advance_act_cursor(
        lane="coe-t2", created_at="2026-06-08T07:00:00+08:00", act_id="prev"
    )

    await record_world_round_close(
        lane="coe-t2",
        advance_cursor_to=None,  # 空批次：不推进游标
        materials_ingested_date="2026-06-08",
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    # 游标保持上一版不动（空批次不推进）
    assert snap.act_cursor_created_at == "2026-06-08T07:00:00+08:00"
    assert snap.act_cursor_act_id == "prev"
    # 底料标记仍落上（即便没新 act）
    assert snap.materials_ingested_date == "2026-06-08"


@pytest.mark.integration
async def test_record_world_round_close_advances_cursor_without_marking_materials(
    world_db,
):
    """当天已纳入过底料 / 今天没底料 → materials_ingested_date 传 None：不改它，只推游标。"""
    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    # 上一版已标记今天纳入过
    await record_world_round_close(
        lane="coe-t2", advance_cursor_to=None, materials_ingested_date="2026-06-08"
    )

    # 同一天后续轮：只推游标，materials_ingested_date 传 None（不重标、保留已有值）
    await record_world_round_close(
        lane="coe-t2",
        advance_cursor_to=("2026-06-08T09:00:00+08:00", "a9"),
        materials_ingested_date=None,
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.act_cursor_created_at == "2026-06-08T09:00:00+08:00"
    assert snap.act_cursor_act_id == "a9"
    # materials_ingested_date 沿用上一版（传 None 不清掉已纳入标记）
    assert snap.materials_ingested_date == "2026-06-08", (
        "materials_ingested_date 传 None 时该保留已有值，不能被清回 None"
    )


@pytest.mark.integration
async def test_record_world_round_close_preserves_next_wake_at(world_db):
    """收口标记底料 / 推游标都保留 next_wake_at（到点时刻是独立调度状态、不被打架）。"""
    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    await set_next_wake_at(lane="coe-t2", next_wake_at="2026-06-08T10:00:00+08:00")

    await record_world_round_close(
        lane="coe-t2",
        advance_cursor_to=("2026-06-08T09:00:00+08:00", "a9"),
        materials_ingested_date="2026-06-08",
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.next_wake_at == "2026-06-08T10:00:00+08:00", (
        "收口不该丢掉 next_wake_at"
    )


@pytest.mark.integration
async def test_record_world_round_close_cold_start_no_snapshot_is_safe(world_db):
    """从没写过 WorldState 的 lane 调收口不抛（冷启容错，对称 advance_act_cursor）。"""
    await record_world_round_close(
        lane="coe-never",
        advance_cursor_to=("2026-06-08T05:00:00+08:00", "a1"),
        materials_ingested_date="2026-06-08",
    )


# ---------------------------------------------------------------------------
# arc_reflected_date —— Task 2b：「当日反思已完成」的收口标记
#
# 反思环节（对表翻页）每日一次：engine 在标记 != 今天（含 None=冷启/部署后首跑）时
# 跑反思，反思**成功**才 mark_arc_reflected 落标记（失败不落、同日后续轮重试）。
# 这个标记必须被所有其他 WorldState 写入点保留（write_world_state /
# set_next_wake_at / advance_act_cursor / record_world_round_close），否则任何一轮
# 收口 / 排醒都会把它冲回 None、当天反复重跑反思——这是本任务最容易出 bug 的点。
# ---------------------------------------------------------------------------


def test_worldstate_has_nullable_arc_reflected_date():
    """WorldState 多一个 arc_reflected_date 字段，nullable，默认 None（从没反思过）。"""
    assert "arc_reflected_date" in WorldState.model_fields
    snap = WorldState(lane="x", world_time="t", detail="d")
    assert snap.arc_reflected_date is None


@pytest.mark.integration
async def test_mark_arc_reflected_writes_date_preserving_everything(world_db):
    """mark_arc_reflected 落标记、保留叙述 / 到点时刻 / 游标 / 底料标记（append 一版）。"""
    from app.world.state import mark_arc_reflected

    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    await set_next_wake_at(lane="coe-t2", next_wake_at="2026-06-10T10:00:00+08:00")
    await record_world_round_close(
        lane="coe-t2",
        advance_cursor_to=("2026-06-10T08:05:00+08:00", "a5"),
        materials_ingested_date="2026-06-10",
    )

    await mark_arc_reflected(lane="coe-t2", date="2026-06-10")

    snap = await read_world_state(lane="coe-t2")
    assert snap is not None
    assert snap.arc_reflected_date == "2026-06-10"
    # 其余字段全部沿用上一版（标记不冲掉任何状态）
    assert snap.detail == "d0"
    assert snap.world_time == "t0"
    assert snap.next_wake_at == "2026-06-10T10:00:00+08:00"
    assert snap.act_cursor_created_at == "2026-06-10T08:05:00+08:00"
    assert snap.act_cursor_act_id == "a5"
    assert snap.materials_ingested_date == "2026-06-10"


@pytest.mark.integration
async def test_mark_arc_reflected_cold_start_persists_mark(world_db):
    """冷启动（无任何 WorldState 行）落标记必须**持久化**——不能 no-op 丢标记。

    反思先于续写跑：冷启动反思成功落标时还没有任何 WorldState 行。若这里 no-op，
    冷启动反思成功的标记就丢了、同日每一轮都重跑反思（违反「每日一次、成功后同日
    不再重复跑」）。修法：插一行最小占位快照承载标记——叙述字段中性空白（真实首版
    叙述仍由续写的 update_world 写）、调度字段全 None（gate / 游标行为与真冷启一致）。
    """
    from app.world.state import mark_arc_reflected

    await mark_arc_reflected(lane="coe-cold", date="2026-06-10")

    snap = await read_world_state(lane="coe-cold")
    assert snap is not None, "冷启动落标记必须持久化（不能 no-op 丢标记）"
    assert snap.arc_reflected_date == "2026-06-10"
    # 占位行中性：不冒充世界叙述、不带任何调度状态
    assert snap.detail == "", "占位行不得冒充世界叙述（detail 空白）"
    assert snap.next_wake_at is None
    assert snap.act_cursor_created_at is None
    assert snap.act_cursor_act_id is None
    assert snap.materials_ingested_date is None


@pytest.mark.integration
async def test_mark_arc_reflected_cold_start_mark_survives_first_real_detail(world_db):
    """冷启动落标 → 续写 update_world 写真实首版 detail → 标记被保留链自然带上。"""
    from app.world.state import mark_arc_reflected

    await mark_arc_reflected(lane="coe-cold", date="2026-06-10")
    await write_world_state(
        lane="coe-cold",
        world_time="2026-06-10T08:35:00+08:00",
        detail="清晨，世界的第一版叙述。",
    )

    snap = await read_world_state(lane="coe-cold")
    assert snap is not None
    assert snap.detail == "清晨，世界的第一版叙述。"
    assert snap.world_time == "2026-06-10T08:35:00+08:00"
    assert snap.arc_reflected_date == "2026-06-10", (
        "续写写真实首版 detail 不该冲掉冷启动落下的反思标记"
    )


@pytest.mark.integration
async def test_write_world_state_preserves_arc_reflected_date(world_db):
    """update_world 改叙述不冲掉当日反思标记。"""
    from app.world.state import mark_arc_reflected

    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    await mark_arc_reflected(lane="coe-t2", date="2026-06-10")

    await write_world_state(lane="coe-t2", world_time="t1", detail="新叙述")

    snap = await read_world_state(lane="coe-t2")
    assert snap.arc_reflected_date == "2026-06-10", (
        "update_world 不该冲掉 arc_reflected_date（否则当天反复重跑反思）"
    )


@pytest.mark.integration
async def test_set_next_wake_at_preserves_arc_reflected_date(world_db):
    """排下次醒（set_next_wake_at）不冲掉当日反思标记。"""
    from app.world.state import mark_arc_reflected

    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    await mark_arc_reflected(lane="coe-t2", date="2026-06-10")

    await set_next_wake_at(lane="coe-t2", next_wake_at="2026-06-10T11:00:00+08:00")

    snap = await read_world_state(lane="coe-t2")
    assert snap.arc_reflected_date == "2026-06-10", (
        "set_next_wake_at 不该冲掉 arc_reflected_date"
    )


@pytest.mark.integration
async def test_advance_act_cursor_preserves_arc_reflected_date(world_db):
    """推进 act 游标不冲掉当日反思标记。"""
    from app.world.state import mark_arc_reflected

    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    await mark_arc_reflected(lane="coe-t2", date="2026-06-10")

    await advance_act_cursor(
        lane="coe-t2", created_at="2026-06-10T09:00:00+08:00", act_id="a9"
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap.arc_reflected_date == "2026-06-10", (
        "advance_act_cursor 不该冲掉 arc_reflected_date"
    )


@pytest.mark.integration
async def test_record_world_round_close_preserves_arc_reflected_date(world_db):
    """收口（推游标 + 标底料）不冲掉当日反思标记——最容易出 bug 的写入点。"""
    from app.world.state import mark_arc_reflected

    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    await mark_arc_reflected(lane="coe-t2", date="2026-06-10")

    await record_world_round_close(
        lane="coe-t2",
        advance_cursor_to=("2026-06-10T09:00:00+08:00", "a9"),
        materials_ingested_date="2026-06-10",
    )

    snap = await read_world_state(lane="coe-t2")
    assert snap.arc_reflected_date == "2026-06-10", (
        "record_world_round_close 不该冲掉 arc_reflected_date（否则每轮收口都把"
        "当日反思标记打回 None、当天反复重跑反思）"
    )
    assert snap.act_cursor_act_id == "a9"
    assert snap.materials_ingested_date == "2026-06-10"


@pytest.mark.integration
async def test_mark_arc_reflected_preserved_then_overwritten_next_day(world_db):
    """跨天再次反思成功 → 标记更新为新一天（append 新版、读最新）。"""
    from app.world.state import mark_arc_reflected

    await write_world_state(lane="coe-t2", world_time="t0", detail="d0")
    await mark_arc_reflected(lane="coe-t2", date="2026-06-10")
    await mark_arc_reflected(lane="coe-t2", date="2026-06-11")

    snap = await read_world_state(lane="coe-t2")
    assert snap.arc_reflected_date == "2026-06-11"
