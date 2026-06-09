"""动作落库这条边 — pull 范式契约基石.

新范式（pull）：角色用 ``act`` 自主做事（``ActPerformed``，自然语言
``description``），但 **act 不再唤醒 world**。life 做完一件事直接
``insert_idempotent(ActPerformed)`` 落 ``data_act_performed`` 表，不 emit、不走
RabbitMQ、不触发任何唤醒。world 按自己 sleep 的节奏醒来时，从"上次消费游标之后"
批量读这段时间攒下的 act 一并推演（见 ``list_recent_acts`` / world engine）。

为什么用 ``insert_idempotent`` 而非 ``insert_append``：act 工具失败重放会用同一
``(lane, act_id)`` 再写一次，``insert_append`` 对无 Version 的 Data 重复插会抛
UniqueViolation（dedup_hash 撞、且明说不 swallow），``insert_idempotent`` 是
``ON CONFLICT DO NOTHING``、重放无害——保留 ``(lane, act_id)`` 幂等。

这里立的是 ``ActPerformed`` 的数据形态 + ``perform_act`` 落库 helper + "落库不唤醒"
这条契约。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.domain.world_events as we_mod
from app.domain.world_events import ActPerformed, perform_act


def test_act_is_durable_not_transient():
    """动作要可持久化（非 transient）—— world 后续要从 PG pull 它。"""
    meta = getattr(ActPerformed, "Meta", None)
    assert not (meta and getattr(meta, "transient", False))


def test_act_carries_what_world_needs():
    """动作的数据形态带齐 world 推演所需：谁做的、做了啥、何时、哪个泳道。"""
    fields = set(ActPerformed.model_fields)
    assert {"lane", "act_id", "persona_id", "description", "occurred_at"} <= fields


def test_act_natural_key_is_lane_and_act_id():
    """自然键是 (lane, act_id)：insert_idempotent 按它去重，lane 泳道隔离硬约束。"""
    from app.runtime.data import key_fields

    assert key_fields(ActPerformed) == ("lane", "act_id")


def test_act_fields_are_all_scalar_str():
    """所有字段都是标量 str —— 这是 ActPerformed 的形态选择：一件做过的事用
    自然语言 description 一句话承载就够，world 直接读这句话推演，不需要结构化
    动作细节。（framework 已支持 dict/list→JSONB 持久化，这里是设计约束、不是
    能力限制。）"""
    for name, fi in ActPerformed.model_fields.items():
        assert fi.annotation is str, f"{name} 必须是 str,实际 {fi.annotation!r}"


@pytest.mark.asyncio
async def test_perform_act_inserts_idempotent_not_emit(monkeypatch):
    """perform_act(...) → insert_idempotent 一条 ActPerformed，字段照传；不 emit。

    pull 范式命门：act 落库但绝不唤醒 world（不 emit、不走 durable publish）。
    """
    fake_insert = AsyncMock(return_value=1)
    fake_emit = AsyncMock()
    monkeypatch.setattr(we_mod, "insert_idempotent", fake_insert)
    monkeypatch.setattr(we_mod, "emit", fake_emit, raising=False)

    await perform_act(
        lane="coe-t1",
        act_id="a1",
        persona_id="akao",
        description="我去厨房做饭",
        occurred_at="2026-06-05T08:00:00Z",
    )

    fake_insert.assert_awaited_once()
    act = fake_insert.await_args.args[0]
    assert isinstance(act, ActPerformed)
    assert act.lane == "coe-t1"
    assert act.act_id == "a1"
    assert act.persona_id == "akao"
    assert act.description == "我去厨房做饭"
    assert act.occurred_at == "2026-06-05T08:00:00Z"
    # 绝不唤醒 world：没有任何 emit
    fake_emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_perform_act_uses_idempotent_not_append(monkeypatch):
    """落库走 insert_idempotent（重放幂等），绝不走 insert_append（重放炸 UniqueViolation）。"""
    fake_insert = AsyncMock(return_value=1)
    fake_append = AsyncMock(return_value=1)
    monkeypatch.setattr(we_mod, "insert_idempotent", fake_insert)
    monkeypatch.setattr(we_mod, "insert_append", fake_append, raising=False)

    await perform_act(
        lane="coe-t1",
        act_id="a1",
        persona_id="akao",
        description="我去厨房做饭",
        occurred_at="2026-06-05T08:00:00Z",
    )

    fake_insert.assert_awaited_once()
    fake_append.assert_not_awaited()
