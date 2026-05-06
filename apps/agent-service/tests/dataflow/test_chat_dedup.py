"""Phase 5a: dedup 层级测试。

ChatTrigger 自身不参与 dedup（source.mq 入口无 insert_idempotent）；
真实 dedup 在 ChatRequest 这一层（in-graph durable wire）。
"""
from __future__ import annotations

import pytest

from app.domain.chat_dataflow import ChatRequest, ChatTrigger
from app.runtime.persist import insert_idempotent
from tests.runtime.conftest import migrate


@pytest.mark.integration
async def test_chat_request_idempotent_blocks_second_emit(test_db):
    """同一 (message_id, persona_id) 的 ChatRequest insert 第二次返 0。

    ChatRequest's joint Key (message_id, persona_id) drives the
    in-graph durable dedup row. mq redelivery hitting chat_node's
    durable consumer triggers insert_idempotent → row already exists
    → handler short-circuits via 0 return.
    """
    await migrate(ChatRequest, test_db)

    r1 = ChatRequest(message_id="m1", persona_id="p1", session_id="s1")
    # same Key, different non-Key field
    r2 = ChatRequest(message_id="m1", persona_id="p1", session_id="s2")

    n1 = await insert_idempotent(r1)
    n2 = await insert_idempotent(r2)

    assert n1 == 1
    assert n2 == 0


@pytest.mark.integration
async def test_chat_request_dedup_per_persona_independent(test_db):
    """Different personas with same message_id should each insert successfully."""
    await migrate(ChatRequest, test_db)

    r_p1 = ChatRequest(message_id="m1", persona_id="p1")
    r_p2 = ChatRequest(message_id="m1", persona_id="p2")

    assert await insert_idempotent(r_p1) == 1
    assert await insert_idempotent(r_p2) == 1


def test_chat_trigger_does_not_have_table():
    """ChatTrigger transient=True -> migrator skips it (filter inside plan_migration)."""
    from app.runtime.migrator import plan_migration

    plan = plan_migration([ChatTrigger], {})
    sql_blob = "\n".join(getattr(s, "sql", "") or str(s) for s in plan.stmts)
    assert "data_chat_trigger" not in sql_blob, (
        "ChatTrigger.Meta.transient=True should make migrator skip table creation"
    )
