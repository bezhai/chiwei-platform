"""persona review 每日补班 — 周级慢钟的钟（cron → 翻译 → sweep 三层）.

周级目标 + 每日补班（同睡前回顾对账班范式，spec 决策 4）：每天 11:00 CST 一班
（避开睡前回顾的 05:00–10:00 对账窗口），逐 persona 预检「本周是否已有
source='review' 的版本」（:func:`app.life.persona_chain.has_review_version_this_week`，
不靠单字段 marker——睡前回顾 marker 事故的教训），没有才进
:func:`run_persona_review`。本周班失败 fail-open，次日自动补；成功的班由周级
幂等挡住。persona 清单从 ``list_all_persona_ids``（bot_persona 表）取——不硬编
三姐妹名字（宪法）。

照 fetch_dataflow 的三层翻译（时间源 Data 必须单字段 ts 的框架硬约束）：
cron 喂单字段 ``PersonaReviewTick``，翻译节点补进程级 lane 后返回
``PersonaReviewSweep``，由 ``@node`` 自动 emit，in-process 接回 sweep 节点。
run_persona_review 自身
fail-open + single_flight + 锁内周级幂等复查——一个 persona 失败绝不影响下一个。
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest

import app.life.persona_review_cron as prc
from app.life.persona_review_cron import (
    PersonaReviewSweep,
    PersonaReviewTick,
    persona_review_sweep_node,
    persona_review_to_sweep_tick,
)

_CST = timezone(timedelta(hours=8))

# 周三 11:00 的补班时刻。
_ELEVEN_AM = datetime(2026, 6, 10, 11, 0, tzinfo=_CST)


@pytest.fixture
def patched(monkeypatch):
    state = {
        "personas": ["akao", "chinagi", "ayana"],
        # 本周已有 review 版本的 persona 集合（预检的闸）。
        "reviewed_this_week": set(),
        "reviews": [],
        "precheck_calls": [],
    }

    async def fake_list_personas():
        return list(state["personas"])

    async def fake_has_review_this_week(*, lane, persona_id, now=None):
        state["precheck_calls"].append(
            {"lane": lane, "persona_id": persona_id, "now": now}
        )
        return persona_id in state["reviewed_this_week"]

    async def fake_review(**kwargs):
        state["reviews"].append(kwargs)

    monkeypatch.setattr(prc, "list_all_persona_ids", fake_list_personas)
    monkeypatch.setattr(prc, "has_review_version_this_week", fake_has_review_this_week)
    monkeypatch.setattr(prc, "run_persona_review", fake_review)
    monkeypatch.setattr(prc.cst_time, "now_cst", lambda: _ELEVEN_AM)
    return state


# ---------------------------------------------------------------------------
# 翻译节点：单字段 tick → 带 lane 的 sweep（时间源硬约束的变速箱）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_translates_to_sweep_with_deployment_lane(monkeypatch):
    emitted = []

    async def fake_emit(data):
        emitted.append(data)

    # @node wrapper 调 runtime.emit，且仍把 Sweep 返回给直接调用方。
    runtime_emit = importlib.import_module("app.runtime.emit")
    monkeypatch.setattr(runtime_emit, "emit", fake_emit)
    monkeypatch.setattr(prc, "current_deployment_lane", lambda: "coe-t2")

    result = await persona_review_to_sweep_tick(
        PersonaReviewTick(ts="2026-06-10T11:00:00+08:00")
    )

    expected = PersonaReviewSweep(lane="coe-t2")
    assert result == expected
    assert emitted == [expected], "@node wrapper 应将单一返回值自动 emit 一次"


@pytest.mark.asyncio
async def test_tick_translation_defaults_lane_to_prod(monkeypatch):
    """LANE 未设（prod 进程）→ lane 归一成 "prod"（与 infra 各处口径一致）。"""
    emitted = []

    async def fake_emit(data):
        emitted.append(data)

    runtime_emit = importlib.import_module("app.runtime.emit")
    monkeypatch.setattr(runtime_emit, "emit", fake_emit)
    monkeypatch.setattr(prc, "current_deployment_lane", lambda: None)

    result = await persona_review_to_sweep_tick(PersonaReviewTick(ts="t"))

    expected = PersonaReviewSweep(lane="prod")
    assert result == expected
    assert emitted == [expected]


# ---------------------------------------------------------------------------
# sweep 节点：逐 persona 预检周级幂等，没有 review 版才进 run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_reviews_every_persona_without_review_this_week(patched):
    """本周都没有 review 版 → 每个 persona 各跑一次，带 (lane, persona, now)。"""
    await persona_review_sweep_node(PersonaReviewSweep(lane="coe-t2"))

    assert [r["persona_id"] for r in patched["reviews"]] == [
        "akao", "chinagi", "ayana",
    ]
    for call in patched["reviews"]:
        assert call["lane"] == "coe-t2"
        assert call["now"] == _ELEVEN_AM


@pytest.mark.asyncio
async def test_sweep_skips_persona_already_reviewed_this_week(patched):
    """预检：本周已有 review 版的 persona 跳过（省一次锁），其余照跑。"""
    patched["reviewed_this_week"].add("chinagi")

    await persona_review_sweep_node(PersonaReviewSweep(lane="coe-t2"))

    assert [r["persona_id"] for r in patched["reviews"]] == ["akao", "ayana"]


@pytest.mark.asyncio
async def test_sweep_precheck_uses_lane_and_now(patched):
    """预检按 (lane, persona, now) 问周级幂等——周界判断用同一触发时刻。"""
    await persona_review_sweep_node(PersonaReviewSweep(lane="coe-t2"))

    assert len(patched["precheck_calls"]) == 3
    for call in patched["precheck_calls"]:
        assert call["lane"] == "coe-t2"
        assert call["now"] == _ELEVEN_AM


@pytest.mark.asyncio
async def test_sweep_uses_persona_registry_not_hardcoded_names(patched):
    """persona 清单来自 list_all_persona_ids（bot_persona 表）——不硬编三姐妹（宪法）。"""
    patched["personas"] = ["someone-new"]

    await persona_review_sweep_node(PersonaReviewSweep(lane="coe-t2"))

    assert [r["persona_id"] for r in patched["reviews"]] == ["someone-new"]


def test_tick_and_sweep_are_transient_signals():
    """两个信号都是 transient（只当唤醒，不落 pg）；tick 满足单字段 ts 约定。"""
    assert PersonaReviewTick.model_fields.keys() == {"ts"}
    assert getattr(PersonaReviewTick.Meta, "transient", False)
    assert getattr(PersonaReviewSweep.Meta, "transient", False)
