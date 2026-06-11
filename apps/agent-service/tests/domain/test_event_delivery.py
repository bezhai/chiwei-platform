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
from app.data.queries.mailbox import deliver_event
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
