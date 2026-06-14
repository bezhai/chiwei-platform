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
    list_persona_npc_speech_in_window,
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
        summary="水壶在响",
        occurred_at="2026-06-03T08:00:00Z",
    )

    unread = await list_unread_events(lane="coe-t1", persona_id="akao")

    assert len(unread) == 1
    ev = unread[0]
    assert ev.event_id == "e1"
    assert ev.kind == "ambient"
    assert ev.source == "world"
    assert ev.summary == "水壶在响"


@pytest.mark.integration
async def test_deliver_then_read_surroundings_kind(mailbox_db):
    """周遭切片（kind=surroundings）durable 投递 → 读回，kind 字段如实保留（1C Task 2）。

    world 五官用 sense 投 kind=surroundings 的周遭切片；life 读回时按 kind 分层呈现
    （周遭进「此刻你周遭」段、动静进动静段），所以 kind 必须在 durable round-trip 里
    如实保留、不被归一成 ambient。
    """
    await deliver_event(
        lane="coe-t1",
        persona_id="ayana",
        event_id="s1",
        kind="surroundings",
        source="world",
        summary="你在客厅写作业，厨房飘来香味。",
        occurred_at="2026-06-03T14:00:00+08:00",
    )

    unread = await list_unread_events(lane="coe-t1", persona_id="ayana")

    assert len(unread) == 1
    assert unread[0].kind == "surroundings"
    assert unread[0].summary == "你在客厅写作业，厨房飘来香味。"


@pytest.mark.integration
async def test_deliver_then_read_speech_kind(mailbox_db):
    """对话原话（kind=speech）durable 投递 → 读回，kind/source 如实保留（1C Task 3）。

    chat 把原话直投收件人信箱（kind=speech、source=说话者 persona_id）。life 读回时
    按 kind 分层呈现成「X 对你说：原话」，所以 kind 与 source 必须 durable round-trip
    保留、不被归一成 ambient。
    """
    await deliver_event(
        lane="coe-t1",
        persona_id="ayana",
        event_id="sp1",
        kind="speech",
        source="akao",
        summary="绫奈姐姐你在做什么好吃的呀",
        occurred_at="2026-06-03T14:00:00+08:00",
    )

    unread = await list_unread_events(lane="coe-t1", persona_id="ayana")

    assert len(unread) == 1
    assert unread[0].kind == "speech"
    assert unread[0].source == "akao", "speech 的 source 是说话者（渲染「X 对你说」要用）"
    assert unread[0].summary == "绫奈姐姐你在做什么好吃的呀", "原话原样保留"


def test_event_envelope_has_no_room_id_field():
    """room_id 已物理删除：EventEnvelope 不再有这个字段。

    room_id 是当初为静态 presence 预留、从不读、恒为空的字段（1C 范式：在场靠
    world 自然语言推演、绝不建结构化在场名单）。物理删除杜绝后人顺手填它复活
    presence。这条钉死字段不存在。
    """
    assert "room_id" not in EventEnvelope.model_fields


def test_deliver_event_rejects_room_id_kwarg():
    """deliver_event 不再接受 room_id 参数（投递面已无 presence 锚点）。"""
    import inspect

    assert "room_id" not in inspect.signature(deliver_event).parameters


@pytest.mark.integration
async def test_mark_read_removes_from_unread(mailbox_db):
    """标已读后，那条 event 不再出现在未读集里。"""
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", summary="s1",
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
        kind="ambient", source="world", summary="prod-evt",
        occurred_at="2026-06-03T08:00:00Z",
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", summary="coe-evt",
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
        kind="ambient", source="world", summary="给赤尾的",
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
        kind="ambient", source="world", summary="s1",
        occurred_at="2026-06-03T08:00:00Z",
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e2",
        kind="ambient", source="world", summary="s2",
        occurred_at="2026-06-03T08:00:01Z",
    )

    # life 读到本轮这一批
    read_batch = await list_unread_events(lane="coe-t1", persona_id="akao")
    read_ids = [e.event_id for e in read_batch]
    assert sorted(read_ids) == ["e1", "e2"]

    # 2. life "想一轮"期间，world 又投进来一条 e3
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e3",
        kind="ambient", source="world", summary="想的时候进来的",
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
            kind="ambient", source="world", summary="s1",
            occurred_at="2026-06-03T08:00:00Z",
        )

    unread = await list_unread_events(lane="coe-t1", persona_id="akao")
    assert len(unread) == 1


@pytest.mark.integration
async def test_unread_ordered_by_occurred_at(mailbox_db):
    """未读批次按发生时间升序返回(life 按时间顺序消化)。"""
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="late",
        kind="ambient", source="world", summary="后发生",
        occurred_at="2026-06-03T09:00:00Z",
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="early",
        kind="ambient", source="world", summary="先发生",
        occurred_at="2026-06-03T08:00:00Z",
    )

    unread = await list_unread_events(lane="coe-t1", persona_id="akao")
    assert [e.event_id for e in unread] == ["early", "late"]


@pytest.mark.integration
async def test_unread_mixed_format_ordered_by_real_instant(mailbox_db):
    """混格式 occurred_at 按真实时刻排序（Unix 毫秒 / ISO 不能字符串序乱排）。

    历史脏数据现实：chat 老链路写 Unix 毫秒（``"1780..."``，以 1 开头），world/life
    写 ISO（``"2026-..."``，以 2 开头）。raw TEXT 字符串序会把 Unix 毫秒整体排在
    ISO 前面（``"1" < "2"``），哪怕那条 Unix 毫秒的真实时刻其实更晚——"按发生先后"
    被打乱、life 看到的顺序错乱。归一到真实时刻排序后，必须按真实先后。

    这里构造：
      * ISO 早（真实 UTC 2026-06-03 00:00）
      * Unix 毫秒晚（真实 UTC 2026-06-03 12:00，字符串以 1 开头）
      * ISO 最晚（真实 UTC 2026-06-03 23:00）
    真实先后应是 [iso_early, unix_mid, iso_late]，而非字符串序的 [unix_mid, ...]。
    """
    from datetime import datetime, timezone

    unix_mid_ms = int(
        datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
    )

    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="iso_late",
        kind="ambient", source="world", summary="最晚-ISO",
        occurred_at="2026-06-03T23:00:00+00:00",
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="unix_mid",
        kind="external", source="user:u1", summary="中间-Unix毫秒",
        occurred_at=str(unix_mid_ms),
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="iso_early",
        kind="ambient", source="world", summary="最早-ISO",
        occurred_at="2026-06-03T00:00:00+00:00",
    )

    unread = await list_unread_events(lane="coe-t1", persona_id="akao")

    assert [e.event_id for e in unread] == ["iso_early", "unix_mid", "iso_late"], (
        "混格式必须按真实时刻排序——Unix 毫秒不能因字符串以 1 开头就被排到 ISO 前面"
    )


@pytest.mark.integration
async def test_npc_speech_in_window_reads_only_npc_speech(mailbox_db):
    """睡前回顾的 NPC 互动证据查询：只捞本生活日窗口内、投给她的 NPC speech event。

    NPC 层第四刀（代码层）的命门——回顾要从信箱里拿到权威的 ``npc:名字`` 机读键
    （而非从被剥过前缀的 transcript 文本里猜）。所以这条查询按窗口 + kind=speech +
    source 以 ``npc:`` 起头三重过滤，**只**返回 NPC 来访：

      * world 环境动静（kind=ambient / surroundings、source=world）排除；
      * 姐妹直投（kind=speech、source=persona_id 如 akao）排除——那是真姐妹，不是 NPC；
      * 真人外部消息（source=user:xxx）排除。

    已读 / 未读都要捞回：回顾在睡前跑，当天的 NPC speech 多半已被 life 标已读了，
    按未读捞会漏掉今天的互动（这是与 list_unread_events 的关键区别——它是窗口读、
    不看 read 表）。
    """
    # 窗口：2026-06-10 全天（CST）
    start_iso = "2026-06-10T04:00:00+08:00"
    end_iso = "2026-06-10T23:30:00+08:00"

    # 1) 窗口内的 NPC 来访（要捞回）——且已被标已读，验证窗口读不看 read 表
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="npc-1",
        kind="speech", source="npc:林小满",
        summary="绫奈周末一起去图书馆复习好不好？",
        occurred_at="2026-06-10T16:30:00+08:00",
    )
    await mark_events_read(lane="coe-t1", persona_id="ayana", event_ids=["npc-1"])

    # 2) 姐妹直投 speech（source=persona_id）——不是 NPC，排除
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="sis-1",
        kind="speech", source="akao",
        summary="绫奈在写作业吗",
        occurred_at="2026-06-10T17:00:00+08:00",
    )
    # 3) world 环境动静——排除
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="amb-1",
        kind="ambient", source="world",
        summary="窗外开始下雨",
        occurred_at="2026-06-10T18:00:00+08:00",
    )
    # 4) 窗口外的 NPC 来访（前一天）——排除
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="npc-old",
        kind="speech", source="npc:林小满",
        summary="昨天的事",
        occurred_at="2026-06-09T16:30:00+08:00",
    )
    # 5) 投给别的姐妹的 NPC 来访——persona 隔离，排除
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="npc-other",
        kind="speech", source="npc:陈鹿",
        summary="给赤尾的",
        occurred_at="2026-06-10T16:30:00+08:00",
    )

    events = await list_persona_npc_speech_in_window(
        lane="coe-t1", persona_id="ayana", start_iso=start_iso, end_iso=end_iso
    )

    assert [e.event_id for e in events] == ["npc-1"], (
        "只捞窗口内、投给 ayana 的 NPC speech（已读也算）；姐妹直投 / 环境动静 / "
        "窗口外 / 别人的都排除"
    )
    assert events[0].source == "npc:林小满", "source 原样保留，回顾据它取 npc:名字 键"
    assert events[0].summary == "绫奈周末一起去图书馆复习好不好？"


@pytest.mark.integration
async def test_npc_speech_in_window_ordered_by_real_instant(mailbox_db):
    """多次 NPC 来访按真实时刻升序（与意识流证据时间序对齐）。"""
    start_iso = "2026-06-10T04:00:00+08:00"
    end_iso = "2026-06-10T23:30:00+08:00"

    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="late",
        kind="speech", source="npc:林小满", summary="后说的",
        occurred_at="2026-06-10T18:00:00+08:00",
    )
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="early",
        kind="speech", source="npc:沈乐", summary="先说的",
        occurred_at="2026-06-10T10:00:00+08:00",
    )

    events = await list_persona_npc_speech_in_window(
        lane="coe-t1", persona_id="ayana", start_iso=start_iso, end_iso=end_iso
    )

    assert [e.event_id for e in events] == ["early", "late"]


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
        kind="ambient", source="world", summary="给赤尾的",
        occurred_at="2026-06-03T08:00:00Z",
    )
    # chinagi：投了一条，但本轮已全部读过（envelope 有、read 也有）
    await deliver_event(
        lane="coe-t1", persona_id="chinagi", event_id="e2",
        kind="ambient", source="world", summary="给千凪的",
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
        kind="ambient", source="world", summary="s1",
        occurred_at="2026-06-03T08:00:00Z",
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e2",
        kind="ambient", source="world", summary="s2",
        occurred_at="2026-06-03T08:00:01Z",
    )
    # 另一 lane 有未读 → 不该出现在 coe-t1 的对账结果里
    await deliver_event(
        lane="prod", persona_id="chinagi", event_id="e3",
        kind="ambient", source="world", summary="prod-evt",
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
        kind="ambient", source="world", summary="s1",
        occurred_at="2026-06-03T08:00:00Z",
    )
    await deliver_event(
        lane="coe-t1", persona_id="chinagi", event_id="e2",
        kind="ambient", source="world", summary="s2",
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
            kind="ambient", source="world",
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


# ---------------------------------------------------------------------------
# 被动 kind 不补敲（权宜修复 v2：codex 发现 wake=False 没挡补敲、修复被绕过）
#
# 背景：world 每推演一轮就用 sense 给三姐妹各投一条周遭切片（kind=surroundings）。
# 上一版给 deliver_event 加 wake=False 只挡了**即时敲门**，没挡 world engine 每轮在
# 到点 gate 之前调的 renotify_unread **补敲**——它通过 list_personas_with_unread 查
# 出"有未读的 persona"挨个补敲。纯 surroundings 入信箱后就是"未读"，于是每轮 world
# tick 的补敲照样把自排睡着的姐妹叫醒，修复被绕过。
#
# 根因：被动语义只在投递瞬间用（wake 参数）、没进入信箱的持久语义。修复把被动语义落
# 在已持久化的 kind 上（PASSIVE_EVENT_KINDS），即时敲门和补敲对账两条路径都读同一处。
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_pure_surroundings_unread_not_renotified(mailbox_db, monkeypatch):
    """承重断言（复现 codex 发现的 bug）：纯 surroundings 未读的 persona **不**被补敲。

    ayana 信箱里只有一条 surroundings（被动周遭切片，sense 投的）、没有真动静。
    renotify_unread（world engine 每轮 tick 在到点 gate 之前调的对账自愈）查"有未读
    的 persona"时**必须**把纯 surroundings 的 ayana 排除——否则每轮 world tick 的补敲
    照样把自排睡着的姐妹叫醒，自排睡眠系统性睡不满（即时敲门改 wake=False 后仍被这条
    补敲路径绕过的真 bug）。

    对比 akao：有一条真动静（ambient notify）未读 → 照常被补敲（真有人找她该唤醒）。
    """
    # ayana：只有一条 surroundings（被动周遭切片，不该被补敲叫醒）
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="s1",
        kind="surroundings", source="world",
        summary="你在客厅写作业，午后的光斜照进来。",
        occurred_at="2026-06-03T14:00:00+08:00",
    )
    # akao：有一条真动静（ambient notify）未读 → 照常该被补敲
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="n1",
        kind="ambient", source="world", summary="玄关传来开关门的声音",
        occurred_at="2026-06-03T14:00:01+08:00",
    )

    emitted: list = []

    async def fake_emit(data):
        emitted.append(data)

    import app.data.queries.mailbox as mailbox_mod

    monkeypatch.setattr(mailbox_mod, "emit", fake_emit)

    count = await renotify_unread(lane="coe-t1")

    assert count == 1, (
        f"只有 akao（真动静）该被补敲，纯 surroundings 的 ayana 不该叫醒，实际补敲 {count}"
    )
    assert [a.persona_id for a in emitted] == ["akao"], (
        f"补敲只该发给真动静的 akao，实际 {[a.persona_id for a in emitted]}"
    )


@pytest.mark.integration
async def test_list_personas_with_unread_excludes_pure_passive(mailbox_db):
    """list_personas_with_unread（补敲对账读侧）：纯被动未读的 persona 不算"有未读"。

    ayana 只有 surroundings 未读 → 不在补敲名单；akao 有真动静 ambient 未读 → 在名单。
    这是上面补敲行为的底层查询断言：补敲对账那条查询排除被动 kind。
    """
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="s1",
        kind="surroundings", source="world", summary="周遭切片",
        occurred_at="2026-06-03T14:00:00+08:00",
    )
    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="n1",
        kind="ambient", source="world", summary="真动静",
        occurred_at="2026-06-03T14:00:01+08:00",
    )

    personas = await list_personas_with_unread(lane="coe-t1")

    assert personas == ["akao"], (
        f"补敲对账只该返回有真动静未读的 akao，排除纯 surroundings 的 ayana，实际 {personas}"
    )


@pytest.mark.integration
async def test_mixed_passive_and_real_unread_persona_is_renotified(mailbox_db):
    """同一 persona 既有 surroundings 又有真动静未读 → 仍被补敲（有真动静就该唤醒）。

    补敲对账只排除"纯被动"未读的 persona——有真动静（哪怕同时混着 surroundings）的
    照常补敲。守住"排除被动"不能误伤"有真动静"这条边界。
    """
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="s1",
        kind="surroundings", source="world", summary="周遭切片",
        occurred_at="2026-06-03T14:00:00+08:00",
    )
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="n1",
        kind="ambient", source="world", summary="厨房有动静",
        occurred_at="2026-06-03T14:00:01+08:00",
    )

    personas = await list_personas_with_unread(lane="coe-t1")

    assert personas == ["ayana"], (
        f"混着真动静的 persona 仍该被补敲，实际 {personas}"
    )


@pytest.mark.integration
async def test_she_wakes_still_reads_surroundings(mailbox_db):
    """她自己醒来读未读（list_unread_events）**仍读得到** surroundings —— 不能断这条。

    补敲对账（list_personas_with_unread）排除被动 kind 是为了不主动叫醒她；但她下次
    自己醒来（self-wake 到点）时仍要在 stimulus 里读到全部未读、含周遭切片。
    list_unread_events 绝不排除 surroundings——和补敲那条查询是分开的两条。
    """
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="s1",
        kind="surroundings", source="world",
        summary="你在客厅写作业，午后的光斜照进来。",
        occurred_at="2026-06-03T14:00:00+08:00",
    )

    unread = await list_unread_events(lane="coe-t1", persona_id="ayana")

    assert [e.event_id for e in unread] == ["s1"]
    assert unread[0].kind == "surroundings"


@pytest.mark.integration
async def test_long_sleep_surroundings_backlog_can_crowd_out_real_events(mailbox_db):
    """codex 隐患（积压挤占）暴露测试：长睡攒的旧 surroundings 排在真动静之前。

    她长睡期间 world 每轮投一条 surroundings，醒来时 list_unread_events 按发生时间
    升序返回（旧的在前）。下游 life_wake 取数有 cap（_LIFE_INBOX_MAX=50，取最旧 N
    条），积压的旧 surroundings 比后来的真 chat / notify 早发生、排在前面，超 cap 时会
    把真动静挤出本轮处理批（真动静留未读、下轮再处理 → 对真人消息的响应延迟）。

    本测试**只暴露排序事实**（旧 surroundings 在真动静之前），不在 mailbox 层硬修——
    修复（真动静优先 / 被动只留最新一条）超出这次权宜范围、且要小心别引入确定性打分。
    见交回说明，列为待办（memory project_world_sense_wake_tradeoff）。
    """
    # 长睡期间攒的多条 surroundings（早发生）
    for i in range(3):
        await deliver_event(
            lane="coe-t1", persona_id="ayana", event_id=f"s{i}",
            kind="surroundings", source="world", summary=f"周遭{i}",
            occurred_at=f"2026-06-03T0{i}:00:00+08:00",
        )
    # 后来才到的真动静（晚发生 → 升序排在 surroundings 之后）
    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="real",
        kind="ambient", source="world", summary="真有人找她",
        occurred_at="2026-06-03T20:00:00+08:00",
    )

    unread = await list_unread_events(lane="coe-t1", persona_id="ayana")
    ids = [e.event_id for e in unread]

    # 暴露隐患：旧 surroundings 全排在真动静之前（cap 截最旧 N 条会先吃掉 surroundings）
    assert ids == ["s0", "s1", "s2", "real"], (
        f"积压隐患：旧 surroundings 排在真动静之前，cap 时会先被取到，实际 {ids}"
    )
