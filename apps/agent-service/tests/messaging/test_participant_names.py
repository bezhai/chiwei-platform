"""参与者的名字就是它在世界里的名字：中文名的收件箱从头到尾都能用。

名字会写进队列名和 routing key（AMQP 客户端只收 ASCII 的队列名，中文名按
:func:`app.messaging.message.broker_form` 写成 ASCII）、去重状态的 edge、记录者的行，死信头里
也带着它（broker 加的 ``x-death`` 记着原队列名和 routing key）。这里用真 broker + 真 Postgres
把每一处都走一遍：发、问、定时送达、没送达的告知、按名字查记录、死信的查看和重放。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS
from app.messaging.lifecycle import start_messaging
from app.messaging.message import Kind
from app.messaging.receiving import inbox
from app.messaging.record import read_record
from app.messaging.sending import ask, send, send_at
from app.runtime.wire import RetryPolicy

from .conftest import LANE
from .helpers import Inbox, eventually, outcomes, outcomes_become

pytestmark = pytest.mark.usefixtures("messaging_db")


async def test_send_to_and_ask_an_inbox_with_a_chinese_name(broker):
    akao = Inbox(answer="在厨房。")
    chinagi = Inbox()
    inbox("赤尾", on_message=akao.on_message, on_question=akao.on_question)
    inbox("千凪", on_message=chinagi.on_message)
    await start_messaging()
    assert await broker.depth(f"inbox_:bgtr75i_{LANE}") == 0
    assert await broker.depth(f"questions_:bgtr75i_{LANE}") == 0

    delivery = await send(sender="千凪", recipient="赤尾", body="姐姐，晚饭好了。")
    answer = await ask(sender="千凪", recipient="赤尾", body="你在哪？", timeout_seconds=10)

    assert delivery.delivered
    await eventually(lambda: akao.got)
    m = akao.got[0]
    assert (m.message_id, m.sender, m.recipient, m.kind) == (
        delivery.message_id,
        "千凪",
        "赤尾",
        Kind.MESSAGE,
    )
    assert answer.answered and answer.text == "在厨房。"
    assert akao.questions[0].sender == "千凪"

    expected = ["sending", "delivered"]
    assert await outcomes_become(delivery.message_id, expected) == expected
    rows = await read_record(participant="赤尾")
    assert {(r["sender"], r["recipient"]) for r in rows} >= {("千凪", "赤尾"), ("赤尾", "千凪")}


async def test_a_scheduled_message_between_chinese_names(broker):
    """定时送达到点转交给中文名的收件箱；对方没开设时，没送达的告知退回给中文名的发送方。"""
    akao = Inbox()
    chinagi = Inbox()
    inbox("赤尾", on_message=akao.on_message)
    inbox("千凪", on_message=chinagi.on_message)
    await start_messaging()

    at = datetime.now(UTC) + timedelta(seconds=1)
    delivered = await send_at(sender="千凪", recipient="赤尾", body="该起床了。", at=at)
    returned = await send_at(sender="千凪", recipient="绫奈", body="快递到了。", at=at)

    await eventually(lambda: akao.got and chinagi.got, timeout=10)
    assert akao.got[0].message_id == delivered
    notice = chinagi.got[0]
    assert notice.kind == Kind.NOT_DELIVERED
    assert (notice.sender, notice.recipient) == ("千凪", "千凪")
    assert "绫奈" in notice.body
    assert outcomes(await read_record(message_id=returned)) == [
        "sending",
        "scheduled",
        "not_delivered",
    ]


async def test_dead_letters_of_a_chinese_inbox_can_be_looked_at_and_replayed(
    broker, monkeypatch
):
    """死信头里记着原队列名和 routing key，都带着中文名：查看时认得出来源，重放回得去。"""
    from app.messaging import receiving
    from app.messaging.dead_letters import peek_dead_letters, replay_dead_letters

    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=1, backoff="linear", base_delay_ms=100, max_delay_ms=100, lease_ms=60_000),
    )
    akao = Inbox(fail_times=1)
    inbox("赤尾", on_message=akao.on_message)
    await start_messaging()

    delivery = await send(sender="operator", recipient="赤尾", body="第一次会失败。")
    dead_letters = f"{ISOLATED_DEAD_LETTERS}_{LANE}"
    await eventually(lambda: broker.depth(dead_letters), timeout=10)

    seen = await peek_dead_letters(limit=10)
    assert [row["message"]["message_id"] for row in seen] == [delivery.message_id]
    assert seen[0]["origin"] == f"inbox_:bgtr75i_{LANE}"
    assert seen[0]["message"]["recipient"] == "赤尾"

    result = await replay_dead_letters(limit=10, operator="test")

    assert result == {"replayed": 1, "refused": 0, "failed": 0}
    await eventually(lambda: akao.got, timeout=10)
    assert akao.got[0].message_id == delivery.message_id
    assert await broker.depth(dead_letters) == 0
