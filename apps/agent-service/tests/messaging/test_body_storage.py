"""正文的规矩跟记录者对得上：拒掉的正文真的存不进记录者的表，收下的那些真的存得进、原样读得回。

记录是发送的一部分（:mod:`app.messaging.record`）：一段存不进去的正文，每一次发送都会在写记录
那一步失败，重试多少次都一样。所以这类正文在造消息时就拒掉（:func:`app.messaging.message.message_body`）。
这里拿真 Postgres 核对两边的口径。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.messaging.message import Kind, Message, SendFailed, message_body
from app.messaging.record import Outcome, read_record, record

pytestmark = pytest.mark.usefixtures("messaging_db")


def _message(message_id: str, body: str) -> Message:
    """直接造，绕过正文校验：看的是记录者自己存不存得下。"""
    return Message(
        message_id=message_id,
        sender="world",
        recipient="akao",
        time=datetime.now(UTC),
        kind=Kind.MESSAGE,
        wakes_recipient=True,
        body=body,
    )


@pytest.mark.parametrize("body", ["门响了\x00一声。", "门响了\ud800一声。"])
async def test_what_message_body_refuses_the_recorder_cannot_store(body):
    with pytest.raises(ValueError):
        message_body(body)
    with pytest.raises(SendFailed):
        await record(_message("refused", body), Outcome.SENDING)


@pytest.mark.parametrize(
    "body",
    ["你听见门响了一声。\n第二行\t有制表符", "外面下雨了 🌧", "\ufffe\uffff 非字符也存得下"],
)
async def test_what_message_body_keeps_the_recorder_stores_and_reads_back_as_it_was(body):
    assert message_body(body) == body

    await record(_message("kept", body), Outcome.SENDING)

    [row] = await read_record(message_id="kept")
    assert row["body"] == body
