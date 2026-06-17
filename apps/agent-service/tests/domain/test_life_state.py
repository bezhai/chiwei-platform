"""LifeState 主观快照持久化契约 — Task 3 (life engine 三姐妹).

LifeState 是某姐妹"此刻"的主观快照：她现在在干嘛 (current_state)、什么情绪
(response_mood)、活动类型 (activity_type) + 更新时间。**没有 state_end_at**——
那是旧 life engine 卡死的根（锁死一个"做到几点"的大状态干等）。

as_latest + Version：每次想完一轮 append 一版，对外读永远取最新一版。Key 带
lane——runtime 持久化不自动加 lane，不显式带上 coe / ppe 泳道就会覆盖 prod 的
"她此刻状态"（写脏线上客观真相，比只读污染更重）。

集成测试（真实 Postgres）：整个正确性故事是 append 出新版本、select_latest 取
最新、lane 隔离——mock pg 等于什么都没测。
"""

from __future__ import annotations

import pytest

from app.domain.life_state import (
    LifeState,
    find_life_state,
    save_life_state,
)
from app.runtime.persist import select_latest
from tests.runtime.conftest import migrate


@pytest.fixture
async def life_state_db(test_db):
    """Build the LifeState table on the test db."""
    await migrate(LifeState, test_db)
    yield test_db


@pytest.mark.integration
async def test_save_then_read_latest(life_state_db):
    """写一版主观快照 → select_latest 读回 current_state/response_mood/activity_type。"""
    await save_life_state(
        lane="coe-t3",
        persona_id="akao",
        current_state="窝在床上刷手机",
        response_mood="慵懒",
        activity_type="rest",
        observed_at="2026-06-03T12:30:00Z",
    )

    latest = await select_latest(LifeState, {"lane": "coe-t3", "persona_id": "akao"})

    assert latest is not None
    assert latest.current_state == "窝在床上刷手机"
    assert latest.response_mood == "慵懒"
    assert latest.activity_type == "rest"
    assert latest.observed_at == "2026-06-03T12:30:00Z"


@pytest.mark.integration
async def test_new_version_supersedes_old(life_state_db):
    """想完新一轮 append 新版，对外读到的是最新那版（不是卡在旧状态）。"""
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="睡觉", response_mood="困", activity_type="sleep",
        observed_at="2026-06-03T08:00:00Z",
    )
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="去厨房找吃的", response_mood="迷糊但饿", activity_type="move",
        observed_at="2026-06-03T12:30:00Z",
    )

    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap is not None
    assert snap.current_state == "去厨房找吃的"
    assert snap.response_mood == "迷糊但饿"
    assert snap.activity_type == "move"


@pytest.mark.integration
async def test_lane_isolation_on_life_state(life_state_db):
    """lane 隔离命门：prod 与 coe 各自的快照绝不互相覆盖 / 互读。"""
    await save_life_state(
        lane="prod", persona_id="akao",
        current_state="prod-状态", response_mood="x", activity_type="a",
        observed_at="2026-06-03T08:00:00Z",
    )
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="coe-状态", response_mood="y", activity_type="b",
        observed_at="2026-06-03T08:00:00Z",
    )

    prod_snap = await find_life_state(lane="prod", persona_id="akao")
    coe_snap = await find_life_state(lane="coe-t3", persona_id="akao")

    assert prod_snap is not None and prod_snap.current_state == "prod-状态"
    assert coe_snap is not None and coe_snap.current_state == "coe-状态"


@pytest.mark.integration
async def test_persona_isolation_on_life_state(life_state_db):
    """三姐妹各自独立快照：akao 的状态不是 chinagi 的状态。"""
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="赤尾在睡", response_mood="x", activity_type="sleep",
        observed_at="2026-06-03T08:00:00Z",
    )

    assert (await find_life_state(lane="coe-t3", persona_id="akao")).current_state == "赤尾在睡"
    assert await find_life_state(lane="coe-t3", persona_id="chinagi") is None


# ---------------------------------------------------------------------------
# 自然键 + next_wake_at dead field（Task 2 删自设闹钟后，next_wake_at 恒 None 不删列）。
# next_wake_at 的写入方测试已随 set_life_next_wake_at 删除（见文件末 Task 2 section）。
# ---------------------------------------------------------------------------


def test_lifestate_key_carries_lane_and_persona():
    """LifeState 自然键必须含 lane + persona_id —— 双键，泳道 + 角色隔离。"""
    from app.runtime.data import key_fields

    keys = key_fields(LifeState)
    assert "lane" in keys
    assert "persona_id" in keys


@pytest.mark.integration
async def test_life_next_wake_at_stays_null_dead_field(life_state_db):
    """next_wake_at 是 dead field：save_life_state 写出的列恒为 NULL（无写真值的路径）。"""
    await save_life_state(
        lane="coe-t3",
        persona_id="akao",
        current_state="窝在床上",
        response_mood="慵懒",
        activity_type="rest",
        observed_at="2026-06-05T20:00:00+08:00",
    )
    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap is not None
    assert snap.next_wake_at is None, "dead field：next_wake_at 恒 None（自设闹钟已删）"


# ---------------------------------------------------------------------------
# day_reviewed_date —— 睡前回顾的当日 marker（additive 列，arc_reflected_date 同款）。
#
# 命门：LifeState 是 append-only、每个写点都整行重写——任何一个写点不沿用
# day_reviewed_date，marker 就被静默清掉 → 同生活日重跑回顾（快班写完、收口一
# update 就丢标 → 凌晨补班再跑一遍）。Task 2 删自设闹钟后写点只剩两个：save_life_state
# 沿用，mark_day_reviewed 落标且保留其余字段（set_life_next_wake_at 写点随闹钟删除）。
# ---------------------------------------------------------------------------


def test_lifestate_has_nullable_day_reviewed_date():
    """LifeState 多一个 ``day_reviewed_date`` 字段，nullable（默认 None=从没回顾过）。"""
    assert "day_reviewed_date" in LifeState.model_fields
    snap = LifeState(
        lane="x",
        persona_id="akao",
        current_state="c",
        response_mood="m",
        activity_type="a",
        observed_at="t",
    )
    assert snap.day_reviewed_date is None


@pytest.mark.integration
async def test_mark_day_reviewed_sets_marker_preserving_all_fields(life_state_db):
    """mark_day_reviewed 落生活日标签，沿用其余主观快照各字段（落标绝不毁别的状态）。"""
    from app.domain.life_state import mark_day_reviewed

    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="躺下睡了", response_mood="平静", activity_type="sleep",
        observed_at="2026-06-10T23:30:00+08:00",
    )

    await mark_day_reviewed(lane="coe-t3", persona_id="akao", date="2026-06-10")

    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap is not None
    assert snap.day_reviewed_date == "2026-06-10"
    # 其余字段全保留（arc_reflected_date 同款教训：落标绝不毁别的状态）
    assert snap.current_state == "躺下睡了"
    assert snap.response_mood == "平静"
    assert snap.activity_type == "sleep"
    assert snap.observed_at == "2026-06-10T23:30:00+08:00"


@pytest.mark.integration
async def test_save_life_state_carries_forward_day_reviewed_date(life_state_db):
    """save_life_state（update 工具写点）append 新版时沿用 day_reviewed_date（不清）。

    场景：快班 23:30 回顾成功落标 → 起夜 03:50 她又醒一轮、update 状态再睡——
    若 save 把 marker 清掉，快班的"同生活日已回顾"失守、起夜那轮再触发一次回顾。
    """
    from app.domain.life_state import mark_day_reviewed

    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="睡了", response_mood="平静", activity_type="sleep",
        observed_at="2026-06-10T23:30:00+08:00",
    )
    await mark_day_reviewed(lane="coe-t3", persona_id="akao", date="2026-06-10")

    # 起夜：update 换状态（没有回顾动作）
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="起夜喝了口水又躺回去", response_mood="迷糊", activity_type="sleep",
        observed_at="2026-06-11T03:50:00+08:00",
    )

    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap.day_reviewed_date == "2026-06-10", (
        "save_life_state 不该清 day_reviewed_date（否则起夜再睡会同生活日重跑回顾）"
    )


@pytest.mark.integration
async def test_mark_day_reviewed_cold_start_no_snapshot_is_safe(life_state_db):
    """从没活过一轮（无 LifeState）时 mark_day_reviewed 安全跳过：不抛、不造占位假状态。

    与 mark_arc_reflected 的冷启占位不同：life 的回顾对同一 target_date 只有两班
    （快班要求 LifeState 存在才可能触发；主班 cron 每个 target_date 只对账一次），
    无快照时没有第二班会重跑同一 target，跳过不丢语义；造空字符串占位快照反而会
    被 life 轮的冷启恢复段当成"上次记得自己在做："喂出怪话。
    """
    from app.domain.life_state import mark_day_reviewed

    await mark_day_reviewed(lane="coe-t3", persona_id="never", date="2026-06-10")

    assert await find_life_state(lane="coe-t3", persona_id="never") is None, (
        "无快照时不造占位假状态"
    )


# ---------------------------------------------------------------------------
# Task 2（纯客观事件驱动范式）：删 life 自设闹钟整条 —— set_life_next_wake_at 删掉。
#
# next_wake_at 是「她自己设的闹钟」（空时间点、维持运转），Task 1 收口后 world 已不再
# 读它判谁该醒（grep state.next_wake_at 在 world engine 零命中），所以没有任何读取方。
# 写入方 set_life_next_wake_at 随之删掉。**列本身保留为 dead field**（不删 Data 列——
# framework migrate 删列 fail-closed 会让 pod crash loop），只是不再有任何写非 None 值
# 的路径，字段恒为 None、不再被读。
# ---------------------------------------------------------------------------


def test_set_life_next_wake_at_is_gone():
    """删自设闹钟：写 next_wake_at 的 set_life_next_wake_at 函数不复存在。"""
    import app.domain.life_state as ls

    assert not hasattr(ls, "set_life_next_wake_at"), (
        "set_life_next_wake_at（自设闹钟唯一写入方）必须删掉——next_wake_at 已无读取方"
    )


def test_next_wake_at_remains_dead_field_default_none():
    """next_wake_at 列保留为 dead field（不删列、默认 None、不再被写真值）。

    红线：framework Data 删列 fail-closed → pod crash loop。所以列保留，只是删掉
    所有写入路径，字段恒为 None。这条钉死「列还在、默认 None」（不删 Data 列）。
    """
    assert "next_wake_at" in LifeState.model_fields, (
        "next_wake_at 列保留为 dead field（删列会被 schema migrate fail-closed 拒绝）"
    )
    snap = LifeState(
        lane="x",
        persona_id="akao",
        current_state="c",
        response_mood="m",
        activity_type="a",
        observed_at="t",
    )
    assert snap.next_wake_at is None, "dead field 默认 None（不再有写真值的路径）"
