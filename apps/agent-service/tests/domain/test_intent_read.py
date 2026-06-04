"""Durable intent 读取查询契约 — world intent 唤醒读全那一批 intent.

codex 致命反馈：intent→world 的 60s 合并闸是 latest-only，只把最后一条 intent
的 payload 透给 world；而 ``IntentRaised`` 的 durable 边已 ack。结果 1min 窗口内
前面几条 intent 对 world **等价丢失**——life "想去厨房 / 出门" 的意图被静默吞掉。

修：world 被 intent 唤醒时不止用闸传进来那一条，而是从 PG 读最近一段时间所有
intent 全部呈现给 world（对称 life 读 mailbox）。这条查询是读侧底座。

这些是 integration 测试（真 Postgres）——正确性全在"durable 行怎么落、时间窗
查询怎么筛"，mock pg 测不到任何东西。

最致命的一条是 ``test_reads_all_intents_in_window_not_just_latest``：它复现 codex
点名的失败——窗口内多条 intent 必须全读回，不是只剩最后一条。
"""

from __future__ import annotations

import pytest

from app.data.queries.intents import list_recent_intents
from app.domain.world_events import IntentRaised
from app.runtime.persist import insert_idempotent
from tests.runtime.conftest import migrate


@pytest.fixture
async def intent_db(test_db):
    """Build the durable intent table on the test db."""
    await migrate(IntentRaised, test_db)
    yield test_db


async def _seed(lane: str, intent_id: str, persona_id: str, summary: str, occurred_at: str):
    # 生产里 life ``raise_intent`` → ``emit(IntentRaised)`` → durable publish，
    # framework 按自然键 (lane, intent_id) 走 ``insert_idempotent`` 落表。测试直接
    # 用同一条持久化原语种数据，对齐生产真实落盘形态。
    await insert_idempotent(
        IntentRaised(
            lane=lane,
            intent_id=intent_id,
            persona_id=persona_id,
            summary=summary,
            occurred_at=occurred_at,
        )
    )


@pytest.mark.integration
async def test_reads_all_intents_in_window_not_just_latest(intent_db):
    """最致命点：窗口内多条 intent 全读回（不是只剩合并闸最后那条）。

    三姐妹在 1min 内各起一个意图（想去厨房 / 出门 / 找谁）。world 被唤醒读这段
    时间的 intent 时，三条必须全在——这正是合并闸 latest-only 丢掉前两条的修复。
    """
    await _seed("coe-t1", "i1", "chinagi", "我想起床去厨房", "2026-06-04T08:00:00+08:00")
    await _seed("coe-t1", "i2", "ayana", "我想出门上学", "2026-06-04T08:00:20+08:00")
    await _seed("coe-t1", "i3", "akao", "我想去找千凪", "2026-06-04T08:00:40+08:00")

    got = await list_recent_intents(lane="coe-t1", since_iso="2026-06-04T07:59:00+08:00")

    summaries = [i.summary for i in got]
    assert summaries == ["我想起床去厨房", "我想出门上学", "我想去找千凪"], (
        f"窗口内三条 intent 必须全读回（按 occurred_at 升序），实际 {summaries}"
    )


@pytest.mark.integration
async def test_since_cutoff_excludes_older_intents(intent_db):
    """``since`` 截断：早于截断点的旧 intent 不读回（防把上几轮处理过的全捞出来）。"""
    await _seed("coe-t1", "old", "chinagi", "很久以前的旧意图", "2026-06-04T07:00:00+08:00")
    await _seed("coe-t1", "new", "akao", "刚刚的新意图", "2026-06-04T08:00:30+08:00")

    got = await list_recent_intents(lane="coe-t1", since_iso="2026-06-04T08:00:00+08:00")

    assert [i.summary for i in got] == ["刚刚的新意图"], (
        "截断点之前的旧 intent 不该读回"
    )


@pytest.mark.integration
async def test_since_compares_real_time_across_offsets(intent_db):
    """跨时区命门：life 写 UTC、world 用 CST，``since`` 必须按真实时刻比、不按字面串。

    life 的 ``occurred_at`` 是 UTC（``2026-06-04T00:00:30+00:00`` = 北京 08:00:30）；
    world 的 since 用 CST（``2026-06-04T08:00:00+08:00``）。两者真实时刻：intent 在
    08:00:30、since 在 08:00:00，所以 intent 该被读回。若按字面串比
    （``00:00:30...`` < ``08:00:00...``）会误判成早于截断、漏掉这条——这正是
    必须 cast 成 timestamptz 真实比的原因。
    """
    # UTC 写入：北京时间 08:00:30
    await _seed("coe-t1", "u1", "akao", "想去厨房", "2026-06-04T00:00:30+00:00")

    # since 用 CST 08:00:00（真实时刻早于 intent）→ 该读回
    got = await list_recent_intents(lane="coe-t1", since_iso="2026-06-04T08:00:00+08:00")
    assert [i.summary for i in got] == ["想去厨房"], (
        "since 必须按真实时刻比（cast timestamptz），不能字面串比导致跨时区漏读"
    )

    # since 用 CST 08:01:00（真实时刻晚于 intent）→ 不该读回
    got2 = await list_recent_intents(lane="coe-t1", since_iso="2026-06-04T08:01:00+08:00")
    assert got2 == [], "真实时刻晚于 intent 的 since 不该读回它"


@pytest.mark.integration
async def test_lane_isolation_on_intents(intent_db):
    """lane 隔离：coe 的 intent 不会被 prod 的 world 读到，反之亦然。"""
    await _seed("prod", "p1", "akao", "prod 意图", "2026-06-04T08:00:10+08:00")
    await _seed("coe-t1", "c1", "akao", "coe 意图", "2026-06-04T08:00:10+08:00")

    prod = await list_recent_intents(lane="prod", since_iso="2026-06-04T07:59:00+08:00")
    coe = await list_recent_intents(lane="coe-t1", since_iso="2026-06-04T07:59:00+08:00")

    assert [i.summary for i in prod] == ["prod 意图"]
    assert [i.summary for i in coe] == ["coe 意图"]


@pytest.mark.integration
async def test_empty_window_returns_empty(intent_db):
    """窗口内没有 intent 返回空（不报错）。"""
    got = await list_recent_intents(lane="coe-t1", since_iso="2026-06-04T08:00:00+08:00")
    assert got == []


@pytest.mark.integration
async def test_returns_full_intent_rows(intent_db):
    """读回的是完整 ``IntentRaised`` 行（persona / summary / intent_id 都在，供 world 裁决）。"""
    await _seed("coe-t1", "i1", "chinagi", "我想去厨房", "2026-06-04T08:00:10+08:00")

    got = await list_recent_intents(lane="coe-t1", since_iso="2026-06-04T07:59:00+08:00")

    assert len(got) == 1
    row = got[0]
    assert isinstance(row, IntentRaised)
    assert row.intent_id == "i1"
    assert row.persona_id == "chinagi"
    assert row.summary == "我想去厨房"
    assert row.lane == "coe-t1"
