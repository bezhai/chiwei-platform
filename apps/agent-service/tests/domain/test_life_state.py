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

from app.domain.life_state import LifeState, find_life_state, save_life_state
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
