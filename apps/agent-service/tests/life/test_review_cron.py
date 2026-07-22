"""清晨对账主班 — 睡前回顾的钟（cron → 翻译 → 对账执行三层）.

主保证是钟（spec 决策 2）：sleep 声明可能整晚不发生（部署丢自排且 life 无心跳），
所以清晨 05:00–10:00 cron 逐小时对账「刚结束的生活日」（窗口内每班 target 都是
前一日标签）。「那天回顾过没有」**看 data_day_page 该 (lane, persona, date) 的页
是否存在**，不比对 marker（事故修复，2026-06-12 prod：清晨回笼觉的快班把单字段
marker 推前到当前生活日，对账班误判前一日未回顾、重跑出重复页）——页缺失才补跑、
已有页跳过。persona 清单从现成的 ``list_all_persona_ids`` 取（bot_persona 表），
**不硬编三姐妹名字**（宪法）；只对**该 lane 有 LifeState 记录**的 persona 跑——
bot_persona 全表里可能有没有 life 的 persona，对它们逐小时对账是空转、语义不对。

照 fetch_dataflow 的三层翻译：cron 喂单字段 ``LifeDayReviewTick``（时间源硬约束），
翻译节点补进程级 lane 后返回 ``LifeDayReviewSweep``，由 ``@node``
自动 emit，in-process 接回对账节点。
run_day_review 自身 fail-open + single_flight + 锁内按页存在性权威复查（仅对账班
trigger="sweep" 生效）——一个 persona 失败绝不影响下一个。
"""

from __future__ import annotations

import importlib
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
        # (persona_id, date) 集合：data_day_page 里已存在页的生活日（对账班的闸）。
        "pages": set(),
        "reviews": [],
    }

    async def fake_list_personas():
        return list(state["personas"])

    async def fake_find(*, lane, persona_id):
        return state["snapshots"].get(persona_id)

    async def fake_page_exists(*, lane, persona_id, date):
        return (persona_id, date) in state["pages"]

    async def fake_review(**kwargs):
        state["reviews"].append(kwargs)

    monkeypatch.setattr(rc, "list_all_persona_ids", fake_list_personas)
    monkeypatch.setattr(rc, "find_life_state", fake_find)
    monkeypatch.setattr(rc, "day_page_exists", fake_page_exists)
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

    # @node wrapper 在 runtime.emit 上自动发射函数返回的 Sweep。
    runtime_emit = importlib.import_module("app.runtime.emit")
    monkeypatch.setattr(runtime_emit, "emit", fake_emit)
    monkeypatch.setattr(rc, "current_deployment_lane", lambda: "coe-t2")

    result = await review_to_sweep_tick(
        LifeDayReviewTick(ts="2026-06-11T05:00:00+08:00")
    )

    expected = LifeDayReviewSweep(lane="coe-t2")
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
    monkeypatch.setattr(rc, "current_deployment_lane", lambda: None)

    result = await review_to_sweep_tick(LifeDayReviewTick(ts="t"))

    expected = LifeDayReviewSweep(lane="prod")
    assert result == expected
    assert emitted == [expected]


# ---------------------------------------------------------------------------
# 对账节点：每个 persona 对账刚结束的生活日
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_reviews_every_persona_for_just_ended_living_day(patched):
    """页都缺失 → 每个 persona 各跑一次（trigger="sweep"），target = 刚结束的生活日。"""
    await day_review_sweep_node(LifeDayReviewSweep(lane="coe-t2"))

    assert [r["persona_id"] for r in patched["reviews"]] == ["akao", "chinagi", "ayana"]
    for call in patched["reviews"]:
        assert call["lane"] == "coe-t2"
        assert call["target_date"] == "2026-06-10", "05:00 对账的是前一日标签的生活日"
        assert call["now"] == _FIVE_AM
        assert call["trigger"] == "sweep", "对账班必须亮明触发源（页存在则绝不重跑）"
        # trace 归组：persona 当天（自然日）的意识流 session id
        assert call["trace_session_id"] == make_session_id(
            "coe-t2", call["persona_id"], "2026-06-11"
        )


@pytest.mark.asyncio
async def test_sweep_skips_persona_whose_page_exists(patched):
    """快班昨晚已写出页的 persona（目标日页存在）跳过，其余照跑（对账语义）。"""
    patched["pages"].add(("chinagi", "2026-06-10"))

    await day_review_sweep_node(LifeDayReviewSweep(lane="coe-t2"))

    assert [r["persona_id"] for r in patched["reviews"]] == ["akao", "ayana"]


@pytest.mark.asyncio
async def test_sweep_not_fooled_by_marker_pushed_forward(patched):
    """prod 事故复现（2026-06-12 akao）：前晚快班回顾了 06-10（页已写），清晨回笼觉
    的快班又把 marker 推前到 06-11——对账班按页存在性判定、照样跳过 06-10，绝不
    重跑出一张重复的 v2 页。"""
    patched["snapshots"]["akao"] = _snapshot("akao", day_reviewed_date="2026-06-11")
    patched["pages"].add(("akao", "2026-06-10"))

    await day_review_sweep_node(LifeDayReviewSweep(lane="coe-t2"))

    assert "akao" not in [r["persona_id"] for r in patched["reviews"]]


@pytest.mark.asyncio
async def test_sweep_ignores_marker_when_page_missing(patched):
    """marker == target 但页不存在 → 照跑：「那天回顾过没有」只看页，不看 marker
    （marker 已降级为观测留痕，单字段回答不了按天的问题）。"""
    patched["snapshots"]["akao"] = _snapshot("akao", day_reviewed_date="2026-06-10")

    await day_review_sweep_node(LifeDayReviewSweep(lane="coe-t2"))

    assert "akao" in [r["persona_id"] for r in patched["reviews"]]


@pytest.mark.asyncio
async def test_sweep_runs_when_page_missing_and_marker_stale(patched):
    """marker 是更早的生活日、目标日页缺失（昨晚没跑成）→ 照跑（补班把昨天补出来）。"""
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
