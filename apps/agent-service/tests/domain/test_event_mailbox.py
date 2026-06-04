"""Durable event mailbox contract — Task 1 (event 流转骨架).

The mailbox is the durable信箱: world / 对话 deliver events into a
per-(lane, persona) inbox; a life reads its own unread batch, thinks,
then marks read **only the event_ids it actually read this round**.

These are integration tests (real Postgres via testcontainers) because the
whole correctness story is about how rows persist and how the unread query
behaves — mocking pg here would test nothing.

The single most load-bearing test is
``test_new_events_during_think_round_stay_unread``: it reproduces the
failure the spec calls out by name — if mark-read blanket-marks by persona
instead of by the exact event_ids read, events that landed *during* the
think round get silently swallowed and the life loops on stale state.
"""

from __future__ import annotations

import pytest

from app.data.queries.mailbox import (
    deliver_event,
    list_unread_events,
    mark_events_read,
)
from app.domain.world_events import EventEnvelope, EventRead
from tests.runtime.conftest import migrate


@pytest.fixture
async def mailbox_db(test_db):
    """Build both mailbox tables (envelope + read-marker) on the test db."""
    await migrate(EventEnvelope, test_db)
    await migrate(EventRead, test_db)
    yield test_db


@pytest.mark.integration
async def test_deliver_then_read_full_loop(mailbox_db):
    """一条 event 投递 → 进信箱 → 被读到（完整闭环最小验证）。"""
    await deliver_event(
        lane="coe-t1",
        persona_id="akao",
        event_id="e1",
        kind="ambient",
        source="world",
        room_id="kitchen",
        summary="水壶在响",
        occurred_at="2026-06-03T08:00:00Z",
    )

    unread = await list_unread_events(lane="coe-t1", persona_id="akao")

    assert len(unread) == 1
    ev = unread[0]
    assert ev.event_id == "e1"
    assert ev.kind == "ambient"
    assert ev.source == "world"
    assert ev.room_id == "kitchen"
    assert ev.summary == "水壶在响"


@pytest.mark.integration
async def test_mark_read_removes_from_unread(mailbox_db):
    """标已读后，那条 event 不再出现在未读集里。"""
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", room_id="", summary="s1",
        occurred_at="2026-06-03T08:00:00Z",
    )
    batch = await list_unread_events(lane="coe-t1", persona_id="akao")
    assert [e.event_id for e in batch] == ["e1"]

    await mark_events_read(
        lane="coe-t1", persona_id="akao", event_ids=[e.event_id for e in batch]
    )

    after = await list_unread_events(lane="coe-t1", persona_id="akao")
    assert after == []


@pytest.mark.integration
async def test_lane_isolation_on_mailbox(mailbox_db):
    """同一 persona、同一 event_id，不同 lane 是互不干扰的两条未读。

    这是 lane 隔离的命门：prod 与 coe 的信箱绝不能互相覆盖 / 互读。
    """
    await deliver_event(
        lane="prod", persona_id="akao", event_id="e1",
        kind="ambient", source="world", room_id="", summary="prod-evt",
        occurred_at="2026-06-03T08:00:00Z",
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", room_id="", summary="coe-evt",
        occurred_at="2026-06-03T08:00:00Z",
    )

    prod_unread = await list_unread_events(lane="prod", persona_id="akao")
    coe_unread = await list_unread_events(lane="coe-t1", persona_id="akao")

    assert [e.summary for e in prod_unread] == ["prod-evt"]
    assert [e.summary for e in coe_unread] == ["coe-evt"]

    # 标 coe 的已读绝不能影响 prod 的未读
    await mark_events_read(lane="coe-t1", persona_id="akao", event_ids=["e1"])
    assert [e.summary for e in await list_unread_events(lane="prod", persona_id="akao")] == [
        "prod-evt"
    ]
    assert await list_unread_events(lane="coe-t1", persona_id="akao") == []


@pytest.mark.integration
async def test_persona_isolation_on_mailbox(mailbox_db):
    """信息差底座：投给 akao 的 event,chinagi 读不到。"""
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", room_id="", summary="给赤尾的",
        occurred_at="2026-06-03T08:00:00Z",
    )

    assert len(await list_unread_events(lane="coe-t1", persona_id="akao")) == 1
    assert await list_unread_events(lane="coe-t1", persona_id="chinagi") == []


@pytest.mark.integration
async def test_new_events_during_think_round_stay_unread(mailbox_db):
    """最致命的正确性点：标已读只标本轮实际读到的 event_id。

    模拟一轮 life 思考：
      1. 投 e1, e2 → life 读到 [e1, e2]
      2. life 想一轮的那几十秒里,world 又投了 e3
      3. life 想完,标已读 —— 只标它本轮读到的 [e1, e2]
      4. e3 必须仍是未读(没被 persona 级全标误吞)

    如果实现按 persona 全标(而不是按本轮 event_id 标),e3 会被静默标
    已读、永远不被消化,life 绕回旧状态卡死。这条测试钉死那个 bug。
    """
    # 1. 本轮开始前已有 e1, e2
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", room_id="", summary="s1",
        occurred_at="2026-06-03T08:00:00Z",
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e2",
        kind="ambient", source="world", room_id="", summary="s2",
        occurred_at="2026-06-03T08:00:01Z",
    )

    # life 读到本轮这一批
    read_batch = await list_unread_events(lane="coe-t1", persona_id="akao")
    read_ids = [e.event_id for e in read_batch]
    assert sorted(read_ids) == ["e1", "e2"]

    # 2. life "想一轮"期间，world 又投进来一条 e3
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e3",
        kind="ambient", source="world", room_id="", summary="想的时候进来的",
        occurred_at="2026-06-03T08:00:05Z",
    )

    # 3. life 想完，只标它本轮实际读到的那批
    await mark_events_read(
        lane="coe-t1", persona_id="akao", event_ids=read_ids
    )

    # 4. e3 必须仍然未读
    still_unread = await list_unread_events(lane="coe-t1", persona_id="akao")
    assert [e.event_id for e in still_unread] == ["e3"], (
        "本轮期间新进的 e3 被误标已读 / 丢失 —— mark_events_read 按 persona "
        "全标而非按本轮 event_id 标，正是 spec 钉死要避免的 bug"
    )


@pytest.mark.integration
async def test_deliver_is_idempotent_on_redelivery(mailbox_db):
    """同一 (lane, persona, event_id) 重复投递只进一条(durable 去重)。"""
    for _ in range(3):
        await deliver_event(
            lane="coe-t1", persona_id="akao", event_id="e1",
            kind="ambient", source="world", room_id="", summary="s1",
            occurred_at="2026-06-03T08:00:00Z",
        )

    unread = await list_unread_events(lane="coe-t1", persona_id="akao")
    assert len(unread) == 1


@pytest.mark.integration
async def test_unread_ordered_by_occurred_at(mailbox_db):
    """未读批次按发生时间升序返回(life 按时间顺序消化)。"""
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="late",
        kind="ambient", source="world", room_id="", summary="后发生",
        occurred_at="2026-06-03T09:00:00Z",
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="early",
        kind="ambient", source="world", room_id="", summary="先发生",
        occurred_at="2026-06-03T08:00:00Z",
    )

    unread = await list_unread_events(lane="coe-t1", persona_id="akao")
    assert [e.event_id for e in unread] == ["early", "late"]
