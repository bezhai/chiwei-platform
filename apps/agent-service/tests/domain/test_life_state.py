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
    set_life_next_wake_at,
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
# next_wake_at —— 阶段 1B Task 2（life 到点 gate 的 state 字段，对称 world）。
# ---------------------------------------------------------------------------


def test_lifestate_has_nullable_next_wake_at():
    """LifeState 多一个 ``next_wake_at`` 字段，nullable（默认 None）。"""
    assert "next_wake_at" in LifeState.model_fields
    snap = LifeState(
        lane="x",
        persona_id="akao",
        current_state="c",
        response_mood="m",
        activity_type="a",
        observed_at="t",
    )
    assert snap.next_wake_at is None, "next_wake_at 默认 None（从没自排过）"


def test_lifestate_key_carries_lane_and_persona():
    """LifeState 自然键必须含 lane + persona_id —— 双键，泳道 + 角色隔离。"""
    from app.runtime.data import key_fields

    keys = key_fields(LifeState)
    assert "lane" in keys
    assert "persona_id" in keys


@pytest.mark.integration
async def test_life_next_wake_at_defaults_null_and_insert_roundtrips(life_state_db):
    """save_life_state 不带 next_wake_at → 列存 NULL（additive nullable 列能 insert+读回）。"""
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
    assert snap.next_wake_at is None, "没自排过时 next_wake_at 为 None"


@pytest.mark.integration
async def test_set_life_next_wake_at_appends_version_preserving_state(life_state_db):
    """set_life_next_wake_at append 一版、写进目标时刻、保留最新主观快照各字段。"""
    await save_life_state(
        lane="coe-t3",
        persona_id="akao",
        current_state="在写作业",
        response_mood="专注",
        activity_type="study",
        observed_at="2026-06-05T21:00:00+08:00",
    )
    target = "2026-06-05T21:30:00+08:00"
    await set_life_next_wake_at(lane="coe-t3", persona_id="akao", next_wake_at=target)

    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap is not None
    assert snap.next_wake_at == target, "next_wake_at 写入后能读回"
    # 主观快照各字段沿用上一版（set 不丢状态）
    assert snap.current_state == "在写作业"
    assert snap.response_mood == "专注"
    assert snap.activity_type == "study"
    assert snap.observed_at == "2026-06-05T21:00:00+08:00"


@pytest.mark.integration
async def test_set_life_next_wake_at_double_key_isolation(life_state_db):
    """双键隔离：写 akao 的 next_wake_at 不影响 chinagi（同 lane 不同 persona）。"""
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="akao 状态", response_mood="x", activity_type="a",
        observed_at="2026-06-05T20:00:00+08:00",
    )
    await save_life_state(
        lane="coe-t3", persona_id="chinagi",
        current_state="chinagi 状态", response_mood="y", activity_type="b",
        observed_at="2026-06-05T20:00:00+08:00",
    )
    await set_life_next_wake_at(
        lane="coe-t3", persona_id="akao", next_wake_at="2026-06-05T21:00:00+08:00"
    )

    akao = await find_life_state(lane="coe-t3", persona_id="akao")
    chinagi = await find_life_state(lane="coe-t3", persona_id="chinagi")
    assert akao.next_wake_at == "2026-06-05T21:00:00+08:00"
    assert chinagi.next_wake_at is None, "另一个 persona 的 next_wake_at 不该被波及"


@pytest.mark.integration
async def test_set_life_next_wake_at_cold_start_no_snapshot_is_safe(life_state_db):
    """从没写过 LifeState 的 (lane, persona) 调 set_life_next_wake_at 不抛（冷启容错）。"""
    await set_life_next_wake_at(
        lane="coe-t3", persona_id="never", next_wake_at="2026-06-06T06:30:00+08:00"
    )


# ---------------------------------------------------------------------------
# 必改 2（codex T3）：save_life_state 不能清掉自排意愿（next_wake_at）。
#
# 旧 bug：模型调 update_life_state → save_life_state append 新版，next_wake_at 默认
# 清成 None。于是 event 唤醒她、她 update 但没重新 schedule 时，之前排的 next_wake_at
# 被清 → 旧 self wake 到期被 gate 判 stale（carried != None）作废 → 她不再自排醒、链断、
# 回到等 event。修：save_life_state append 新版时**沿用上一版的 next_wake_at**（不清），
# 与 set_life_next_wake_at 沿用主观字段对称——两个写路径各改各字段、沿用对方最新值。
# 只有 schedule（set）改 next_wake_at，update（save）不毁自排意愿。
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_save_life_state_carries_forward_next_wake_at(life_state_db):
    """save_life_state append 新版时沿用上一版的 next_wake_at（不清成 None）。

    红：现状 save 不带 next_wake_at → 列默认 None，把已排的自排意愿清掉。绿：沿用
    上一版的 next_wake_at —— 只有 schedule（set_life_next_wake_at）能改它。
    """
    # 先排好一个自排时刻（set 写进 next_wake_at）
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="在写作业", response_mood="专注", activity_type="study",
        observed_at="2026-06-05T21:00:00+08:00",
    )
    target = "2026-06-05T21:30:00+08:00"
    await set_life_next_wake_at(lane="coe-t3", persona_id="akao", next_wake_at=target)

    # 再调 update_life_state（save）换状态、但没重新 schedule
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="换了个姿势接着写", response_mood="平静", activity_type="study",
        observed_at="2026-06-05T21:05:00+08:00",
    )

    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap is not None
    # 状态确实换了
    assert snap.current_state == "换了个姿势接着写"
    # 自排意愿不被清：next_wake_at 沿用上一版（命门）
    assert snap.next_wake_at == target, (
        "save_life_state 不该清掉 next_wake_at（否则旧 self wake 被判 stale、链断）"
    )


@pytest.mark.integration
async def test_event_interrupt_without_reschedule_keeps_self_wake_valid(life_state_db):
    """event 打断（update 不 schedule）后，旧 self wake 到期仍放行（carried target 仍 == 保留值）。

    模拟："她自排好 21:30 醒 → 21:10 来个 event 把她唤醒、她 update 状态但没 reschedule"。
    旧 bug：update 把 next_wake_at 清 None → 21:30 那条 self wake 到期 gate 判 stale
    （carried=21:30 != None）作废、不再自排。修后：next_wake_at 沿用 = 21:30，self wake
    携带的 21:30 == state 当前值 → gate 放行（自排链不被 event 打断毁掉）。
    """
    from app.nodes.life_wake import LifeWakeTick, _life_self_wake_gate_passes
    from app.infra import cst_time
    from datetime import timedelta

    # 用相对现实时刻，让 gate 的"到点"判定可控（target 设在过去 1s = 已到点）
    target = (cst_time.now_cst() - timedelta(seconds=1)).isoformat()

    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="在写作业", response_mood="专注", activity_type="study",
        observed_at="2026-06-05T21:00:00+08:00",
    )
    await set_life_next_wake_at(lane="coe-t3", persona_id="akao", next_wake_at=target)

    # event 打断：update 换状态、不 reschedule
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="被门铃打断、应付了一下", response_mood="略烦", activity_type="move",
        observed_at="2026-06-05T21:10:00+08:00",
    )

    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap.next_wake_at == target, "event 打断 update 后 next_wake_at 不被清"

    # 旧 self wake 到期：carried target == 保留的 next_wake_at → gate 放行
    tick = LifeWakeTick(
        lane="coe-t3", persona_id="akao", reason="self", target_wake_at=target
    )
    assert _life_self_wake_gate_passes(
        tick, next_wake_at=snap.next_wake_at, now=cst_time.now_cst()
    ) is True, "event 打断未 reschedule 时，旧 self wake 到期仍应放行（自排不被毁）"


@pytest.mark.integration
async def test_event_round_reschedule_makes_old_self_stale_new_self_valid(life_state_db):
    """event 轮重新 schedule：旧 self stale、新 self 有效；收口顺序最终落新 target。

    一轮 event 唤醒里收口顺序是 update（save，沿用旧 next_wake_at）→ schedule
    （set，写新 target）。最终 state.next_wake_at 必须是新 target：旧 self wake 携带旧
    target 到期判 stale（!= 新值），新 self wake 携带新 target 放行。验收口顺序正确：
    一轮 update + schedule 收口 set 最终是新 target。
    """
    from app.nodes.life_wake import LifeWakeTick, _life_self_wake_gate_passes
    from app.infra import cst_time
    from datetime import timedelta

    old_target = (cst_time.now_cst() - timedelta(seconds=2)).isoformat()
    new_target = (cst_time.now_cst() - timedelta(seconds=1)).isoformat()
    assert old_target != new_target

    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="在写作业", response_mood="专注", activity_type="study",
        observed_at="2026-06-05T21:00:00+08:00",
    )
    await set_life_next_wake_at(lane="coe-t3", persona_id="akao", next_wake_at=old_target)

    # 一轮收口：先 update（save，沿用 old_target）→ 再 schedule（set，写 new_target）
    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="想了想接着写", response_mood="专注", activity_type="study",
        observed_at="2026-06-05T21:10:00+08:00",
    )
    await set_life_next_wake_at(lane="coe-t3", persona_id="akao", next_wake_at=new_target)

    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap.next_wake_at == new_target, "收口顺序：update 后再 schedule，最终落新 target"

    now = cst_time.now_cst()
    old_tick = LifeWakeTick(
        lane="coe-t3", persona_id="akao", reason="self", target_wake_at=old_target
    )
    new_tick = LifeWakeTick(
        lane="coe-t3", persona_id="akao", reason="self", target_wake_at=new_target
    )
    # 旧 self wake 携带 old_target != 当前 new_target → 判 stale 作废
    assert _life_self_wake_gate_passes(
        old_tick, next_wake_at=snap.next_wake_at, now=now
    ) is False, "重 schedule 后旧 self wake 必须判 stale"
    # 新 self wake 携带 new_target == 当前值 → 放行
    assert _life_self_wake_gate_passes(
        new_tick, next_wake_at=snap.next_wake_at, now=now
    ) is True, "重 schedule 后新 self wake 有效"


# ---------------------------------------------------------------------------
# day_reviewed_date —— 睡前回顾的当日 marker（additive 列，arc_reflected_date 同款）。
#
# 命门：LifeState 是 append-only、每个写点都整行重写——任何一个写点不沿用
# day_reviewed_date，marker 就被静默清掉 → 同生活日重跑回顾（快班写完、收口一
# update 就丢标 → 凌晨补班再跑一遍）。所以三个写点全测：save_life_state /
# set_life_next_wake_at 沿用，mark_day_reviewed 落标且保留其余字段。
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
    """mark_day_reviewed 落生活日标签，沿用其余全部字段（主观快照 + next_wake_at 都不丢）。"""
    from app.domain.life_state import mark_day_reviewed

    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="躺下睡了", response_mood="平静", activity_type="sleep",
        observed_at="2026-06-10T23:30:00+08:00",
    )
    target = "2026-06-11T07:00:00+08:00"
    await set_life_next_wake_at(lane="coe-t3", persona_id="akao", next_wake_at=target)

    await mark_day_reviewed(lane="coe-t3", persona_id="akao", date="2026-06-10")

    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap is not None
    assert snap.day_reviewed_date == "2026-06-10"
    # 其余字段全保留（arc_reflected_date 同款教训：落标绝不毁别的状态）
    assert snap.current_state == "躺下睡了"
    assert snap.response_mood == "平静"
    assert snap.activity_type == "sleep"
    assert snap.observed_at == "2026-06-10T23:30:00+08:00"
    assert snap.next_wake_at == target, "落 marker 不能丢自排意愿"


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
async def test_set_life_next_wake_at_carries_forward_day_reviewed_date(life_state_db):
    """set_life_next_wake_at（schedule 收口写点）沿用 day_reviewed_date（不清）。"""
    from app.domain.life_state import mark_day_reviewed

    await save_life_state(
        lane="coe-t3", persona_id="akao",
        current_state="睡了", response_mood="平静", activity_type="sleep",
        observed_at="2026-06-10T23:30:00+08:00",
    )
    await mark_day_reviewed(lane="coe-t3", persona_id="akao", date="2026-06-10")

    await set_life_next_wake_at(
        lane="coe-t3", persona_id="akao", next_wake_at="2026-06-11T07:00:00+08:00"
    )

    snap = await find_life_state(lane="coe-t3", persona_id="akao")
    assert snap.day_reviewed_date == "2026-06-10", (
        "set_life_next_wake_at 不该清 day_reviewed_date"
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
