"""Durable act 读取查询契约 — world act 唤醒读全那一批动作.

新范式：角色用 ``act`` 自主做事（``ActPerformed``，自然语言 ``description``），
durable 回灌唤醒 world 去推演客观结果。intent→world 的 60s 合并闸是 latest-only，
只把最后一条 act 的 payload 透给 world；前面几条对 world 等价丢失。修：world 被
act 唤醒时从 PG 读最近一段时间所有 act 全部呈现给 world（对称 life 读 mailbox）。
:func:`list_recent_acts` 是读侧底座。

这些是 integration 测试（真 Postgres）——正确性全在"durable 行怎么落、时间窗
查询怎么筛"，mock pg 测不到任何东西。
"""

from __future__ import annotations

import pytest

from app.data.queries.acts import list_recent_acts
from app.domain.world_events import ActPerformed
from app.runtime.persist import insert_idempotent
from tests.runtime.conftest import migrate


@pytest.fixture
async def act_db(test_db):
    """Build the durable act table on the test db."""
    await migrate(ActPerformed, test_db)
    yield test_db


async def _seed(lane: str, act_id: str, persona_id: str, description: str, occurred_at: str):
    # 生产里 life 做完一件事 → ``perform_act`` → ``emit(ActPerformed)`` → durable
    # publish，framework 按自然键 (lane, act_id) 走 ``insert_idempotent`` 落表。测试
    # 直接用同一条持久化原语种数据，对齐生产真实落盘形态。
    await insert_idempotent(
        ActPerformed(
            lane=lane,
            act_id=act_id,
            persona_id=persona_id,
            description=description,
            occurred_at=occurred_at,
        )
    )


@pytest.mark.integration
async def test_reads_all_acts_in_window_not_just_latest(act_db):
    """最致命点：窗口内多条 act 全读回（不是只剩合并闸最后那条）。

    三姐妹在 1min 内各做了一件事。world 被唤醒读这段时间的 act 时，三条必须
    全在——这正是合并闸 latest-only 丢掉前两条的修复。
    """
    await _seed("coe-t1", "a1", "chinagi", "我起床去厨房煮咖啡", "2026-06-04T08:00:00+08:00")
    await _seed("coe-t1", "a2", "ayana", "我出门上学", "2026-06-04T08:00:20+08:00")
    await _seed("coe-t1", "a3", "akao", "我去找千凪", "2026-06-04T08:00:40+08:00")

    got = await list_recent_acts(lane="coe-t1", since_iso="2026-06-04T07:59:00+08:00")

    descriptions = [a.description for a in got]
    assert descriptions == ["我起床去厨房煮咖啡", "我出门上学", "我去找千凪"], (
        f"窗口内三条 act 必须全读回（按 occurred_at 升序），实际 {descriptions}"
    )


@pytest.mark.integration
async def test_since_cutoff_excludes_older_acts(act_db):
    """``since`` 截断：早于截断点的旧 act 不读回（防把上几轮处理过的全捞出来）。"""
    await _seed("coe-t1", "old", "chinagi", "很久以前做的事", "2026-06-04T07:00:00+08:00")
    await _seed("coe-t1", "new", "akao", "刚刚做的事", "2026-06-04T08:00:30+08:00")

    got = await list_recent_acts(lane="coe-t1", since_iso="2026-06-04T08:00:00+08:00")

    assert [a.description for a in got] == ["刚刚做的事"], (
        "截断点之前的旧 act 不该读回"
    )


@pytest.mark.integration
async def test_since_compares_real_time_across_offsets(act_db):
    """跨时区命门：life 可能写 UTC、world 用 CST，``since`` 必须按真实时刻比、不按字面串。

    act 的 ``occurred_at`` 可能是 UTC（``2026-06-04T00:00:30+00:00`` = 北京 08:00:30）；
    world 的 since 用 CST（``2026-06-04T08:00:00+08:00``）。两者真实时刻：act 在
    08:00:30、since 在 08:00:00，所以 act 该被读回。若按字面串比会误判成早于截断、
    漏掉这条——这正是必须 cast 成 timestamptz 真实比的原因。
    """
    await _seed("coe-t1", "u1", "akao", "去厨房", "2026-06-04T00:00:30+00:00")

    got = await list_recent_acts(lane="coe-t1", since_iso="2026-06-04T08:00:00+08:00")
    assert [a.description for a in got] == ["去厨房"], (
        "since 必须按真实时刻比（cast timestamptz），不能字面串比导致跨时区漏读"
    )

    got2 = await list_recent_acts(lane="coe-t1", since_iso="2026-06-04T08:01:00+08:00")
    assert got2 == [], "真实时刻晚于 act 的 since 不该读回它"


@pytest.mark.integration
async def test_lane_isolation_on_acts(act_db):
    """lane 隔离：coe 的 act 不会被 prod 的 world 读到，反之亦然。"""
    await _seed("prod", "p1", "akao", "prod 动作", "2026-06-04T08:00:10+08:00")
    await _seed("coe-t1", "c1", "akao", "coe 动作", "2026-06-04T08:00:10+08:00")

    prod = await list_recent_acts(lane="prod", since_iso="2026-06-04T07:59:00+08:00")
    coe = await list_recent_acts(lane="coe-t1", since_iso="2026-06-04T07:59:00+08:00")

    assert [a.description for a in prod] == ["prod 动作"]
    assert [a.description for a in coe] == ["coe 动作"]


@pytest.mark.integration
async def test_empty_window_returns_empty(act_db):
    """窗口内没有 act 返回空（不报错）。"""
    got = await list_recent_acts(lane="coe-t1", since_iso="2026-06-04T08:00:00+08:00")
    assert got == []


@pytest.mark.integration
async def test_returns_full_act_rows(act_db):
    """读回的是完整 ``ActPerformed`` 行（persona / description / act_id 都在，供 world 推演）。"""
    await _seed("coe-t1", "a1", "chinagi", "我去厨房做饭", "2026-06-04T08:00:10+08:00")

    got = await list_recent_acts(lane="coe-t1", since_iso="2026-06-04T07:59:00+08:00")

    assert len(got) == 1
    row = got[0]
    assert isinstance(row, ActPerformed)
    assert row.act_id == "a1"
    assert row.persona_id == "chinagi"
    assert row.description == "我去厨房做饭"
    assert row.lane == "coe-t1"
