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
    list_personas_with_unread,
    list_unread_events,
    mark_events_read,
    renotify_unread,
)
from app.domain.world_events import EventArrived, EventEnvelope, EventRead
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


@pytest.mark.integration
async def test_list_personas_with_unread_only_returns_unread(mailbox_db):
    """信箱对账查询：只返回该 lane 下还有未读 event 的 persona。

    akao 有一条未读 event；chinagi 投了一条但已全部标已读。对账查询必须只返回
    akao（有未读），绝不返回 chinagi（已读完）。这是唤醒自愈回路的反连接命门：
    只补敲信箱里真有未读的人。
    """
    # akao：有一条未读 event（模拟敲门曾失败，envelope 在但没人读）
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", room_id="", summary="给赤尾的",
        occurred_at="2026-06-03T08:00:00Z",
    )
    # chinagi：投了一条，但本轮已全部读过（envelope 有、read 也有）
    await deliver_event(
        lane="coe-t1", persona_id="chinagi", event_id="e2",
        kind="ambient", source="world", room_id="", summary="给千凪的",
        occurred_at="2026-06-03T08:00:01Z",
    )
    await mark_events_read(lane="coe-t1", persona_id="chinagi", event_ids=["e2"])

    personas = await list_personas_with_unread(lane="coe-t1")

    assert personas == ["akao"], (
        f"对账查询只该返回还有未读的 akao，实际 {personas}"
    )


@pytest.mark.integration
async def test_list_personas_with_unread_distinct_and_lane_scoped(mailbox_db):
    """对账查询去重（一人多条未读只算一次）且按 lane 隔离。"""
    # akao 在 coe-t1 有两条未读 → distinct 后只算一个 persona
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
    # 另一 lane 有未读 → 不该出现在 coe-t1 的对账结果里
    await deliver_event(
        lane="prod", persona_id="chinagi", event_id="e3",
        kind="ambient", source="world", room_id="", summary="prod-evt",
        occurred_at="2026-06-03T08:00:02Z",
    )

    personas = await list_personas_with_unread(lane="coe-t1")

    assert personas == ["akao"], (
        f"同一 persona 多条未读应去重、且只看本 lane，实际 {personas}"
    )


@pytest.mark.integration
async def test_renotify_unread_reemits_for_unread_personas(mailbox_db, monkeypatch):
    """信箱对账自愈：对每个有未读的 persona 补敲一次 EventArrived，返回补敲数。

    模拟敲门曾彻底丢失——envelope 在信箱里、没人醒。对账函数查出有未读的 persona、
    挨个补 emit EventArrived，让下游 life-wake 有机会被重新唤醒。已读完的 persona
    不补敲。
    """
    # akao：有未读（敲门曾失败的场景）；chinagi：已读完
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", room_id="", summary="s1",
        occurred_at="2026-06-03T08:00:00Z",
    )
    await deliver_event(
        lane="coe-t1", persona_id="chinagi", event_id="e2",
        kind="ambient", source="world", room_id="", summary="s2",
        occurred_at="2026-06-03T08:00:01Z",
    )
    await mark_events_read(lane="coe-t1", persona_id="chinagi", event_ids=["e2"])

    emitted: list = []

    async def fake_emit(data):
        emitted.append(data)

    import app.data.queries.mailbox as mailbox_mod

    monkeypatch.setattr(mailbox_mod, "emit", fake_emit)

    count = await renotify_unread(lane="coe-t1")

    assert count == 1, f"只有 akao 有未读、该补敲 1 个，实际 {count}"
    assert len(emitted) == 1
    arrived = emitted[0]
    assert isinstance(arrived, EventArrived)
    assert arrived.lane == "coe-t1"
    assert arrived.persona_id == "akao"


@pytest.mark.integration
async def test_stranded_event_recovered_by_renotify(mailbox_db, monkeypatch):
    """完整事故链复现：deliver 落库成功但敲门抛错 → event stranded → renotify 捞回。

    这正是 coe 真机翻车的链：world ``deliver_event`` 先 insert EventEnvelope
    （成功），紧接着 ``emit(EventArrived)`` 撞上瞬时 redis ConnectionError 失败，
    异常被上游 source loop 吞掉，event 永久躺信箱没人读、life 一次没醒。修复靠
    ``renotify_unread``：它从 PG 未读行查出 stranded 的 persona 补敲，不依赖那次
    emit 成功。现有用例是拆开测组件，这条把"敲门失败 → 补救"整条链串起来。
    """
    from redis.exceptions import ConnectionError as RedisConnectionError

    import app.data.queries.mailbox as mailbox_mod

    # 1. deliver 时敲门抛瞬时错（模拟 redis reset）：insert 成功在前、emit 失败在后
    async def emit_boom(data):
        raise RedisConnectionError("Connection reset by peer")

    monkeypatch.setattr(mailbox_mod, "emit", emit_boom)

    with pytest.raises(RedisConnectionError):
        await deliver_event(
            lane="coe-t1", persona_id="akao", event_id="e1",
            kind="ambient", source="world", room_id="kitchen",
            summary="厨房飘来饭菜香", occurred_at="2026-06-03T12:00:00Z",
        )

    # 2. event 已 durable 落库（敲门丢了、信箱里有信），没人读过 → stranded
    stranded = await list_unread_events(lane="coe-t1", persona_id="akao")
    assert [e.event_id for e in stranded] == ["e1"]
    assert await list_personas_with_unread(lane="coe-t1") == ["akao"]

    # 3. 下一轮 world 心跳对账：emit 恢复正常，renotify 把 stranded 补敲回来
    emitted: list = []

    async def emit_ok(data):
        emitted.append(data)

    monkeypatch.setattr(mailbox_mod, "emit", emit_ok)

    count = await renotify_unread(lane="coe-t1")

    assert count == 1, f"stranded 的 akao 该被补敲 1 次，实际 {count}"
    assert len(emitted) == 1 and isinstance(emitted[0], EventArrived)
    assert emitted[0].lane == "coe-t1"
    assert emitted[0].persona_id == "akao"
