"""读书收件箱，跑在真 broker + 真 Postgres 上：她拿起一个文件 → 一条消息进 ``reading`` 收件箱 →
读一程 → 写下 ``FileRead``。

收件箱由 living 插件开设：宿主按 agent-service 的清单只起 skills 和 living 两个插件（只跑
setup），之后通信机制照线上那样开始接收。所以这里用的是插件实际登记的选项：最多处理 1 次、
处理时限 5 分钟。她手机上的文件和读一程本身（取字节、解码、模型调用）换成替身；读一程的替身
在"模型调用失败"时返回 ``None``，跟 :func:`app.living.reading_agent.run_reading_round` 把失败吞掉
的样子一样。
"""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS, lane_queue
from app.living import reading as reading_mod
from app.living.reading import FileRead, SentFile, read_so_far
from app.living.residents import names as names_mod
from app.living.services import READING
from app.messaging import receiving
from app.messaging.broker import inbox_route
from app.messaging.lifecycle import start_messaging
from app.messaging.record import read_record
from tests.living.conftest import RESIDENT_NAMES, her_plugins, her_tool

# 真 broker（带延时插件）+ 通信机制那几张表，跟 ``tests/messaging`` 用同一份。
from tests.messaging.conftest import (  # noqa: F401 — 形参名就是 fixture 名
    LANE,
    broker,
    delayed_broker,
    messaging_db,
)
from tests.messaging.helpers import eventually
from tests.runtime.conftest import migrate

read_a_bit = her_tool("read_a_bit")

pytestmark = pytest.mark.integration

_CST = dt.timezone(dt.timedelta(hours=8))

_BOOK = SentFile(
    attachment_id="msg-1:key-a",
    title="斜阳.txt",
    tos_file="files/key-a",
    who="千凪",
    at=dt.datetime(2026, 7, 25, 20, 10, tzinfo=_CST),
    where="私聊「千凪」",
    still_gettable=True,
)

_INBOX = lane_queue(inbox_route(READING).queue, LANE)
_DEAD_LETTERS = lane_queue(ISOLATED_DEAD_LETTERS, LANE)


class _Round:
    """读一程的替身：记下每一次调用，交回用例给的结果（``None`` 是这一程没读成）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.result = None

    def gives(self, *, impression: str, pages_read: int) -> _Round:
        from app.living.reading_agent import ReadingResult

        self.result = ReadingResult(impression=impression, pages_read=pages_read, finished=False)
        return self

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
async def reading_inbox(broker, messaging_db, living_db, app_host, monkeypatch):  # noqa: F811
    """living 插件开好的读书收件箱在本泳道上收消息；她手机上有一个文件。交回读一程的替身。"""

    async def find_persona(persona_id: str):
        return SimpleNamespace(persona_id=persona_id, display_name=RESIDENT_NAMES[persona_id])

    async def on_her_phone(*, persona_id: str, now: dt.datetime) -> list[SentFile]:
        return [_BOOK]

    await migrate(FileRead, living_db)
    monkeypatch.setattr(names_mod, "find_persona", find_persona)
    monkeypatch.setattr(reading_mod, "files_sent_to", on_her_phone)
    one_round = _Round()
    monkeypatch.setattr(reading_mod, "run_reading_round", one_round)

    await app_host("agent-service", her_plugins())
    await start_messaging()
    return one_round


async def _she_picks_it_up(in_a_moment, *, times: int = 1) -> None:
    async with in_a_moment("akao", lane=LANE):
        for _ in range(times):
            said = await read_a_bit.invoke({"which": "斜阳"})
            assert isinstance(said, str), said


async def _claim(test_db, message_id: str) -> dict | None:
    async with test_db.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT state, attempts FROM runtime_inflight "
                "WHERE edge_id = :e AND idempotent_key = :k"
            ),
            {"e": f"inbox:{READING}@{LANE}", "k": message_id},
        )
        found = row.mappings().first()
        return dict(found) if found else None


async def _the_one_message_id() -> str:
    rows = await read_record(participant=READING, limit=10)
    ids = {r["message_id"] for r in rows}
    assert len(ids) == 1, rows
    return ids.pop()


async def _settled(test_db, message_id: str, state: str) -> None:
    async def reached() -> bool:
        claim = await _claim(test_db, message_id)
        return claim is not None and claim["state"] == state

    await eventually(reached, timeout=15)


async def test_a_file_she_picks_up_is_read_from_the_reading_inbox(
    reading_inbox, in_a_moment, test_db
):
    reading_inbox.gives(impression="太宰那个调子，我读着有点上头。", pages_read=7)

    await _she_picks_it_up(in_a_moment)

    await eventually(
        lambda: read_so_far(lane=LANE, persona_id="akao", attachment_id=_BOOK.attachment_id),
        timeout=15,
    )
    mark = await read_so_far(lane=LANE, persona_id="akao", attachment_id=_BOOK.attachment_id)
    assert (mark.impression, mark.pages_read) == ("太宰那个调子，我读着有点上头。", 7)
    (call,) = reading_inbox.calls
    assert (call["tos_file"], call["file_name"], call["round_id"]) == (
        _BOOK.tos_file,
        _BOOK.title,
        mark.round_id,
    )
    rows = await read_record(message_id=mark.round_id)
    assert [(r["sender"], r["recipient"], r["outcome"]) for r in rows] == [
        (RESIDENT_NAMES["akao"], READING, "sending"),
        (RESIDENT_NAMES["akao"], READING, "delivered"),
    ]
    await _settled(test_db, mark.round_id, "succeeded")


async def test_the_same_round_picked_up_twice_is_read_once(
    reading_inbox, in_a_moment, test_db, broker  # noqa: F811
):
    """同一轮里拿起两次：两条消息同一个 id，第二份在领取时被挡掉（或者看到第一份还拿着、隔一秒
    再看时被挡掉），读一程只跑一次。"""
    reading_inbox.gives(impression="读了一程。", pages_read=7)

    await _she_picks_it_up(in_a_moment, times=2)

    message_id = await _the_one_message_id()
    await _settled(test_db, message_id, "succeeded")
    # 第二份要是碰上第一份还拿着，隔 1 秒再看一次；等过这一次。
    await asyncio.sleep(receiving.HELD_CHECK_FIRST_MS / 1000 + 1.5)
    assert len(reading_inbox.calls) == 1, "同一程被读了两遍 —— 白烧一次钱"
    assert await broker.depth(_INBOX) == 0
    assert await broker.depth(_DEAD_LETTERS) == 0


async def test_a_round_whose_impression_cannot_be_written_goes_to_the_dead_letters_after_one_try(
    reading_inbox, in_a_moment, test_db, broker, monkeypatch  # noqa: F811
):
    """读完了，写 ``FileRead`` 时提交失败：这个错越过了读一程，处理函数抛出。读书收件箱最多处理
    1 次，所以消息直接进本泳道的死信，读一程（模型）只跑过一次，没有排任何重试。"""
    reading_inbox.gives(impression="读完了却写不下来。", pages_read=7)

    async def commit_fails(*args, **kwargs):
        raise RuntimeError("COMMIT failed: the connection was closed")

    monkeypatch.setattr(reading_mod, "insert_append", commit_fails)
    retries: list[dict] = []
    real_publish = receiving.publish

    async def recorded(route, body, *, headers, delay_ms=None):
        retries.append(dict(headers))
        await real_publish(route, body, headers=headers, delay_ms=delay_ms)

    monkeypatch.setattr(receiving, "publish", recorded)

    await _she_picks_it_up(in_a_moment)

    await eventually(lambda: broker.depth(_DEAD_LETTERS), timeout=15)
    message_id = await _the_one_message_id()
    await asyncio.sleep(1.0)
    assert len(reading_inbox.calls) == 1, "失败之后又读了一遍"
    assert await broker.depth(_DEAD_LETTERS) == 1
    assert await broker.depth(_INBOX) == 0
    assert retries == [], f"排了重试：{retries}"
    claim = await _claim(test_db, message_id)
    # 进了死信：重放之前同一程的任何一份都不再读。
    assert (claim["state"], claim["attempts"]) == ("dead_lettered", 1)
    assert (
        await read_so_far(lane=LANE, persona_id="akao", attachment_id=_BOOK.attachment_id)
        is None
    )


async def test_a_round_whose_model_call_fails_is_acknowledged_and_not_dead_lettered(
    reading_inbox, in_a_moment, test_db, broker  # noqa: F811
):
    """读一程的模型调用失败，在读一程里吞掉（返回 ``None``）：印象和页号不动，处理函数正常返回，
    消息确认，不进死信。她下次自己想读了再拿起来。"""
    reading_inbox.result = None

    await _she_picks_it_up(in_a_moment)

    message_id = await _the_one_message_id()
    await _settled(test_db, message_id, "succeeded")
    assert len(reading_inbox.calls) == 1
    assert await broker.depth(_DEAD_LETTERS) == 0
    assert (
        await read_so_far(lane=LANE, persona_id="akao", attachment_id=_BOOK.attachment_id)
        is None
    )
