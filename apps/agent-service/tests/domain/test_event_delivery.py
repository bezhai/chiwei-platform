"""投递→敲门这条边 — Task 1.

``deliver_event`` 是 event 进信箱的唯一入口（world 投递、对话回灌都走它）。
它做两件事：写 durable 信箱条目 + 投递成功后 emit 一个 ``EventArrived`` 敲门
信号唤醒对应 persona 的 life。

去重命中（同一 event 重投）时不再敲门——没有新东西进信箱，没必要唤醒。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.data.queries.mailbox as mailbox_mod
from app.data.queries.mailbox import deliver_event, list_unread_events
from app.domain.world_events import EventArrived, EventEnvelope, EventRead
from tests.runtime.conftest import migrate


@pytest.fixture
async def mailbox_db(test_db):
    await migrate(EventEnvelope, test_db)
    await migrate(EventRead, test_db)
    yield test_db


@pytest.mark.integration
async def test_deliver_emits_knock_for_target_persona(mailbox_db, monkeypatch):
    """新投递一条 event → emit EventArrived(lane, persona) 唤醒那个 persona。"""
    fake_emit = AsyncMock()
    monkeypatch.setattr(mailbox_mod, "emit", fake_emit)

    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", summary="s1",
        occurred_at="2026-06-03T08:00:00Z",
    )

    fake_emit.assert_awaited_once()
    knock = fake_emit.await_args.args[0]
    assert isinstance(knock, EventArrived)
    assert knock.lane == "coe-t1"
    assert knock.persona_id == "akao"


@pytest.mark.integration
async def test_duplicate_delivery_does_not_knock(mailbox_db, monkeypatch):
    """同一 event 重投（去重命中）不敲门——没有新东西进信箱。"""
    fake_emit = AsyncMock()
    monkeypatch.setattr(mailbox_mod, "emit", fake_emit)

    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", summary="s1",
        occurred_at="2026-06-03T08:00:00Z",
    )
    await deliver_event(  # 同一 (lane, persona, event_id) 重投
        lane="coe-t1", persona_id="akao", event_id="e1",
        kind="ambient", source="world", summary="s1",
        occurred_at="2026-06-03T08:00:00Z",
    )

    assert fake_emit.await_count == 1  # 只有第一次新投递敲了门


@pytest.mark.integration
async def test_passive_kind_delivery_lands_in_inbox_without_knock(mailbox_db, monkeypatch):
    """权宜修复 v2（被动语义落在持久化 kind 上）：被动 kind（surroundings）投递只落信箱、**不**敲门。

    prod 节奏失控的根因：world 每推演一轮就用 sense 给三姐妹各投一条周遭切片
    （kind=surroundings），若走唤醒通道（emit EventArrived → 永远放行）会把自排睡着的
    姐妹全敲醒、自排睡眠系统性睡不满。修复把被动语义落在已持久化的 kind 上
    （PASSIVE_EVENT_KINDS）：deliver_event 按 kind 判断要不要敲门——被动 kind 只落信箱
    当**被动上下文**（她下次自己醒来时 list_unread_events 自然读到），不 emit 唤醒。

    上一版的 ``wake=False`` 参数已删（它只能覆盖即时敲门这一条路径、是不完整抽象——
    没挡住 world engine 每轮调的 renotify_unread 补敲，修复被绕过；现统一由 kind 表达，
    即时敲门和补敲对账都读同一处）。``sense`` 投 kind=surroundings 本就被动、不再传 wake。

    承重断言：被动 kind 投递后 ① 信箱里**有**这条 surroundings（list_unread 读得到，
    被动上下文不丢）；② 但**没有** emit 任何 EventArrived（不唤醒）。
    """
    fake_emit = AsyncMock()
    monkeypatch.setattr(mailbox_mod, "emit", fake_emit)

    await deliver_event(
        lane="coe-t1", persona_id="ayana", event_id="s1",
        kind="surroundings", source="world",
        summary="你在客厅写作业，厨房飘来香味。",
        occurred_at="2026-06-03T14:00:00+08:00",
    )

    # ② 不敲门：被动 kind 不唤醒（这是 prod 节奏失控的修复点）
    fake_emit.assert_not_awaited()
    # ① 仍入信箱：她下次自己醒来时 list_unread 读得到（被动上下文不丢）
    unread = await list_unread_events(lane="coe-t1", persona_id="ayana")
    assert len(unread) == 1
    assert unread[0].event_id == "s1"
    assert unread[0].kind == "surroundings"
    assert unread[0].summary == "你在客厅写作业，厨房飘来香味。"


@pytest.mark.integration
async def test_active_kind_delivery_still_knocks(mailbox_db, monkeypatch):
    """真动静（非被动 kind：ambient / speech / external）**仍**敲门唤醒——通道分离只改被动 kind。

    所有非 PASSIVE_EVENT_KINDS 的 event（notify ambient / npc_visit speech / 真人 chat
    external / 日程到点 reminder）行为不变，照常 emit EventArrived 唤醒——"真有人找她 /
    真有动静"该立刻响应，不能被这次权宜修复动到。
    """
    fake_emit = AsyncMock()
    monkeypatch.setattr(mailbox_mod, "emit", fake_emit)

    await deliver_event(
        lane="coe-t1", persona_id="akao", event_id="n1",
        kind="ambient", source="world", summary="玄关传来开关门的声音",
        occurred_at="2026-06-03T08:00:00Z",
    )

    fake_emit.assert_awaited_once()
    knock = fake_emit.await_args.args[0]
    assert isinstance(knock, EventArrived)
    assert knock.persona_id == "akao"
