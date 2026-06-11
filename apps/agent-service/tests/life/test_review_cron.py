"""清晨对账主班 — 睡前回顾的钟（cron → 翻译 → 对账执行三层）.

主保证是钟（spec 决策 2）：sleep 声明可能整晚不发生（部署丢自排且 life 无心跳），
所以清晨 05:00–10:00 cron 逐小时对账「刚结束的生活日」（窗口内每班 target 都是
前一日标签）：marker 未落则跑回顾、已落由 marker 幂等挡住。persona 清单从现成的
``list_all_persona_ids`` 取（bot_persona 表），**不硬编三姐妹名字**（宪法）；
只对**该 lane 有 LifeState 记录**的 persona 跑——bot_persona 全表里可能有没有
life 的 persona，对它们逐小时对账是空转、语义不对。

照 fetch_dataflow 的三层翻译：cron 喂单字段 ``LifeDayReviewTick``（时间源硬约束），
翻译节点补进程级 lane 后 emit ``LifeDayReviewSweep``，in-process 接回对账节点。
run_day_review 自身 fail-open + single_flight + 锁内 marker 对账——一个 persona
失败绝不影响下一个。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.life.review_cron as rc
from app.agent.trace import make_session_id
from app.domain.life_state import LifeState
from app.life.review_cron import (
    LifeDayReviewSweep,
    LifeDayReviewTick,
    day_review_sweep_node,
    review_to_sweep_tick,
)

_CST = timezone(timedelta(hours=8))

# 05:00 对账时刻：刚结束的生活日是 2026-06-10。
_FIVE_AM = datetime(2026, 6, 11, 5, 0, tzinfo=_CST)


def _snapshot(persona_id, **kwargs) -> LifeState:
    base = {
        "lane": "coe-t2",
        "persona_id": persona_id,
        "current_state": "睡着",
        "response_mood": "平静",
        "activity_type": "sleep",
        "observed_at": "2026-06-10T23:30:00+08:00",
    }
    base.update(kwargs)
    return LifeState(**base)


@pytest.fixture
def patched(monkeypatch):
    state = {
        "personas": ["akao", "chinagi", "ayana"],
        # persona_id -> LifeState | None。默认三人都有 LifeState（活过）——
        # 没有 LifeState 的 persona 会被对账节点过滤（专门的用例钉这条）。
        "snapshots": {p: _snapshot(p) for p in ["akao", "chinagi", "ayana"]},
        "reviews": [],
    }

    async def fake_list_personas():
        return list(state["personas"])

    async def fake_find(*, lane, persona_id):
        return state["snapshots"].get(persona_id)

    async def fake_review(**kwargs):
        state["reviews"].append(kwargs)

    monkeypatch.setattr(rc, "list_all_persona_ids", fake_list_personas)
    monkeypatch.setattr(rc, "find_life_state", fake_find)
    monkeypatch.setattr(rc, "run_day_review", fake_review)
    monkeypatch.setattr(rc.cst_time, "now_cst", lambda: _FIVE_AM)
    return state


# ---------------------------------------------------------------------------
# 翻译节点：单字段 tick → 带 lane 的 sweep（时间源硬约束的变速箱）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_translates_to_sweep_with_deployment_lane(monkeypatch):
    emitted = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(rc, "emit", fake_emit)
    monkeypatch.setattr(rc, "current_deployment_lane", lambda: "coe-t2")

    await review_to_sweep_tick(LifeDayReviewTick(ts="2026-06-11T05:00:00+08:00"))

    assert emitted == [LifeDayReviewSweep(lane="coe-t2")]


@pytest.mark.asyncio
async def test_tick_translation_defaults_lane_to_prod(monkeypatch):
    """LANE 未设（prod 进程）→ lane 归一成 "prod"（与 infra 各处口径一致）。"""
    emitted = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(rc, "emit", fake_emit)
    monkeypatch.setattr(rc, "current_deployment_lane", lambda: None)

    await review_to_sweep_tick(LifeDayReviewTick(ts="t"))

    assert emitted == [LifeDayReviewSweep(lane="prod")]


# ---------------------------------------------------------------------------
# 对账节点：每个 persona 对账刚结束的生活日
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_reviews_every_persona_for_just_ended_living_day(patched):
    """marker 都未落 → 每个 persona 各跑一次，target = 刚结束的生活日（前一日标签）。"""
    await day_review_sweep_node(LifeDayReviewSweep(lane="coe-t2"))

    assert [r["persona_id"] for r in patched["reviews"]] == ["akao", "chinagi", "ayana"]
    for call in patched["reviews"]:
        assert call["lane"] == "coe-t2"
        assert call["target_date"] == "2026-06-10", "05:00 对账的是前一日标签的生活日"
        assert call["now"] == _FIVE_AM
        # trace 归组：persona 当天（自然日）的意识流 session id
        assert call["trace_session_id"] == make_session_id(
            "coe-t2", call["persona_id"], "2026-06-11"
        )


@pytest.mark.asyncio
async def test_sweep_skips_persona_already_reviewed(patched):
    """快班昨晚已回顾的 persona（marker == target）跳过，其余照跑（对账语义）。"""
    patched["snapshots"]["chinagi"] = _snapshot(
        "chinagi", day_reviewed_date="2026-06-10"
    )

    await day_review_sweep_node(LifeDayReviewSweep(lane="coe-t2"))

    assert [r["persona_id"] for r in patched["reviews"]] == ["akao", "ayana"]


@pytest.mark.asyncio
async def test_sweep_runs_when_marker_is_stale(patched):
    """marker 是更早的生活日（昨晚没标上）→ 照跑（补班把昨天补出来）。"""
    patched["snapshots"]["akao"] = _snapshot("akao", day_reviewed_date="2026-06-09")

    await day_review_sweep_node(LifeDayReviewSweep(lane="coe-t2"))

    assert "akao" in [r["persona_id"] for r in patched["reviews"]]


@pytest.mark.asyncio
async def test_sweep_uses_persona_registry_not_hardcoded_names(patched):
    """persona 清单来自 list_all_persona_ids（bot_persona 表）——不硬编三姐妹（宪法）。"""
    patched["personas"] = ["someone-new"]
    patched["snapshots"] = {"someone-new": _snapshot("someone-new")}

    await day_review_sweep_node(LifeDayReviewSweep(lane="coe-t2"))

    assert [r["persona_id"] for r in patched["reviews"]] == ["someone-new"]


@pytest.mark.asyncio
async def test_sweep_skips_persona_without_life_state(patched):
    """该 lane 没有 LifeState 的 persona（bot_persona 全表里没有 life 的那些）→
    过滤掉不跑：没有生活日可对账，窗口逐小时对它空转语义不对。"""
    del patched["snapshots"]["chinagi"]  # chinagi 在这个 lane 从没活过一轮

    await day_review_sweep_node(LifeDayReviewSweep(lane="coe-t2"))

    assert [r["persona_id"] for r in patched["reviews"]] == ["akao", "ayana"]


def test_tick_and_sweep_are_transient_signals():
    """两个信号都是 transient（只当唤醒，不落 pg）；tick 满足单字段 ts 约定。"""
    assert LifeDayReviewTick.model_fields.keys() == {"ts"}
    assert getattr(LifeDayReviewTick.Meta, "transient", False)
    assert getattr(LifeDayReviewSweep.Meta, "transient", False)
