"""环顾四周：她问 world 她这里现在什么样、有谁在，world 怎么答她就拿到什么；没人答就如实说没人答。

问题以她的名义发出（她在世界里的名字），里面写着她此刻在哪、在做什么，取自她自己的位置。这一条
不能省：她这一轮做的事要等这一轮结束才汇总发给 world，同一轮里她刚换了地方就环顾四周，world 还
不知道她挪了。

这里的 world 是替身：只管问题那一面（记下问了什么、怎么答），不碰 world 的任何实现。最后两条走真的
通信机制，world 那边用一个只会照稿回答的收件箱代替。
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.living import moment as moment_mod
from app.living import participants as participants_mod
from app.living.moment import look_around, run_moment
from app.living.participants import WORLD
from app.living.whereabouts import note_whereabouts
from app.messaging.message import Answer, SendFailed
from tests.living.conftest import (
    RESIDENT_NAMES,
    model_facing_text,
    names_of_places_in,
    path_samples,
)
from tests.living.test_moment import (  # noqa: F401 — 形参名就是 fixture 名
    moment_db,
    stub_moment,
)

# 真 broker（带延时插件）+ 通信机制那几张表，跟 ``tests/messaging`` 用同一份。
from tests.messaging.conftest import (  # noqa: F401 — 形参名就是 fixture 名
    broker,
    delayed_broker,
    messaging_db,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

# world 照稿回答的一段：多行，带引号和尖括号——回答原样交给她，一个字都不该动。
_ANSWERED = '厨房里灯亮着，锅里的水快开了。\n千凪站在灶台边切葱，说了句"马上好"。<窗外在下小雨>'


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


async def _stand(persona: str, place: str, doing: str, at: dt.datetime) -> None:
    await note_whereabouts(
        lane=LANE,
        persona_id=persona,
        moment_id=f"before:{at.isoformat(timespec='minutes')}",
        place=place,
        doing=doing,
        noted_at=at,
    )


class World:
    """替身 world 的问题那一面：记下她问的每一句，按设定回答、不回答、出错或者一直不返回。

    签名跟 :func:`app.messaging.sending.ask` 一样。"""

    def __init__(self) -> None:
        self.asked: list[dict] = []
        self.answer: str | None = _ANSWERED
        self.reason: str | None = None
        self.raises: Exception | None = None
        self.hangs = False
        self.on_ask = None

    async def ask(self, *, sender, recipient, body, timeout_seconds, message_id=None):
        self.asked.append(
            {
                "sender": sender,
                "recipient": recipient,
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.on_ask is not None:
            self.on_ask()
        if self.hangs:
            await asyncio.Event().wait()
        if self.raises is not None:
            raise self.raises
        return Answer("q-1", self.answer, self.reason)


@pytest.fixture
def world(monkeypatch) -> World:
    stand_in = World()
    monkeypatch.setattr(moment_mod, "ask", stand_in.ask)
    return stand_in


async def _look(in_a_moment, persona: str = "akao") -> object:
    async with in_a_moment(persona, lane=LANE, now=_at(21, 30)):
        return await look_around.invoke({})


# ---------------------------------------------------------------------------
# 问什么、以谁的名义问
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_asks_world_as_herself_where_she_is_and_what_she_is_doing(
    moment_db, world, in_a_moment  # noqa: F811
):
    await _stand("akao", "家/客厅", "翻胶片相册", _at(21))

    await _look(in_a_moment)

    (asked,) = world.asked
    assert (asked["sender"], asked["recipient"]) == ("赤尾", WORLD)
    assert "家/客厅" in asked["body"] and "翻胶片相册" in asked["body"], asked["body"]


@pytest.mark.integration
async def test_the_names_on_the_question_come_from_the_residents_mapping(
    moment_db, world, in_a_moment, monkeypatch  # noqa: F811
):
    """名字就是地址，取自启动时读好的那份对照；代码里不写任何一个名字。"""
    renamed = {persona_id: f"{name}2" for persona_id, name in RESIDENT_NAMES.items()}
    monkeypatch.setattr(participants_mod, "_known", participants_mod.Residents(renamed))
    await _stand("ayana", "家/二楼", "画画", _at(21))

    await _look(in_a_moment, "ayana")

    (asked,) = world.asked
    assert (asked["sender"], asked["recipient"]) == ("绫奈2", WORLD)


@pytest.mark.integration
async def test_the_answer_reaches_her_word_for_word(
    moment_db, world, in_a_moment  # noqa: F811
):
    await _stand("akao", "家/厨房", "等水开", _at(21))

    seen = await _look(in_a_moment)

    assert seen == _ANSWERED


@pytest.mark.integration
async def test_after_switching_in_this_round_she_asks_about_where_she_is_now(
    moment_db, stub_moment, world, post  # noqa: F811
):
    """同一轮里先换了地方再环顾四周：问的是新地方。这一轮的汇总这时还没发出去，world 只能从问题里
    知道她挪了。"""
    await _stand("akao", "家/客厅", "看书", _at(21))
    told_before_asking: list[int] = []
    world.on_ask = lambda: told_before_asking.append(len(post.sent))
    stub_moment(
        ("switch_to", {"doing": "煮乌冬", "place": "家/厨房", "because": "饿了"}),
        ("look_around", {}),
    )

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))

    (asked,) = world.asked
    assert "家/厨房" in asked["body"] and "煮乌冬" in asked["body"], asked["body"]
    assert "家/客厅" not in asked["body"] and "看书" not in asked["body"], asked["body"]
    assert told_before_asking == [0], "前提不成立：问之前 world 已经收到了这一轮的汇总"


@pytest.mark.integration
async def test_after_moving_in_this_round_she_asks_about_the_new_place_still_doing_the_same(
    moment_db, stub_moment, world  # noqa: F811
):
    await _stand("akao", "家/客厅", "看书", _at(21))
    stub_moment(("move_to", {"place": "家/阳台"}), ("look_around", {}))

    await run_moment(lane=LANE, persona_id="akao", now=_at(21, 30))

    (asked,) = world.asked
    assert "家/阳台" in asked["body"] and "看书" in asked["body"], asked["body"]
    assert "家/客厅" not in asked["body"], asked["body"]


@pytest.mark.integration
async def test_before_she_has_stood_anywhere_nothing_is_asked(
    moment_db, world, in_a_moment  # noqa: F811
):
    """没有"这里"可问：她自己都没定下在哪，world 也不知道。先让她落个位置。"""
    seen = await _look(in_a_moment)

    assert world.asked == []
    assert isinstance(seen, str) and "switch_to" in seen, seen


# ---------------------------------------------------------------------------
# 没人回答：如实说没人回答，不编
# ---------------------------------------------------------------------------


def _nobody_answered(seen: object) -> None:
    assert seen == moment_mod.NOBODY_ANSWERED, seen
    assert "没人回答" in seen


@pytest.mark.integration
@pytest.mark.parametrize(
    "reason",
    [
        "45 秒内没有回答",  # 等到截止时刻也没回答
        "对方没有开设收件箱",  # world 没在跑：问题根本没送出去
        "对方没有给出回答",
        "对方回答时失败：RuntimeError",
        None,
    ],
)
async def test_no_answer_tells_her_nobody_answered(
    moment_db, world, in_a_moment, reason  # noqa: F811
):
    await _stand("akao", "家/客厅", "看书", _at(21))
    world.answer, world.reason = None, reason

    _nobody_answered(await _look(in_a_moment))


@pytest.mark.integration
@pytest.mark.parametrize(
    "failure",
    [SendFailed("broker 没有确认", message_id="q-1"), RuntimeError("连不上 broker")],
)
async def test_asking_that_fails_tells_her_nobody_answered(
    moment_db, world, in_a_moment, failure  # noqa: F811
):
    await _stand("akao", "家/客厅", "看书", _at(21))
    world.raises = failure

    _nobody_answered(await _look(in_a_moment))


@pytest.mark.integration
async def test_asking_that_hangs_is_given_up_on_and_tells_her_nobody_answered(
    moment_db, world, in_a_moment, monkeypatch  # noqa: F811
):
    """问题发不出去、一直挂着（broker 不应答）：等到她的上限就不等了，不占着她这一轮。"""
    monkeypatch.setattr(moment_mod, "LOOK_AROUND_ANSWER_SECONDS", 0.05)
    monkeypatch.setattr(moment_mod, "SEND_SECONDS", 0.05)
    await _stand("akao", "家/客厅", "看书", _at(21))
    world.hangs = True

    async with asyncio.timeout(5):
        _nobody_answered(await _look(in_a_moment))


@pytest.mark.integration
async def test_she_waits_for_the_answer_as_long_as_the_tool_says(
    moment_db, world, in_a_moment  # noqa: F811
):
    await _stand("akao", "家/客厅", "看书", _at(21))

    await _look(in_a_moment)

    (asked,) = world.asked
    assert asked["timeout_seconds"] == moment_mod.LOOK_AROUND_ANSWER_SECONDS


# ---------------------------------------------------------------------------
# 交给她的字
# ---------------------------------------------------------------------------


def test_the_look_around_hand_offers_no_place_or_person_names():
    """举出来的名字就是词表，她会逐字抄走：说明里一个地名、一个人名都不给。"""
    for where, text in model_facing_text(look_around).items():
        assert not path_samples(text), f"{where}里摆着路径样本：{text}"
        assert not names_of_places_in(text), f"{where}里写着具体地名：{text}"
        names = [n for n in (*RESIDENT_NAMES, *RESIDENT_NAMES.values()) if n in text]
        assert names == [], f"{where}里写着人名 {names}：{text}"
        assert "例如" not in text and "「" not in text, f"{where}里在举例：{text}"


# ---------------------------------------------------------------------------
# 真的通信机制
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_through_messaging_world_answers_her_question_and_she_reads_it_as_is(
    broker, messaging_db, moment_db, in_a_moment  # noqa: F811
):
    """world 那边用一个照稿回答的收件箱代替：它收到的是她以自己的名字问的、带着她在哪在做什么的
    问题，它的回答原样到了她手上。"""
    from app.messaging.lifecycle import start_messaging
    from app.messaging.receiving import inbox
    from tests.messaging.helpers import Inbox

    stand_in = Inbox(answer=_ANSWERED)
    inbox(WORLD, on_message=stand_in.on_message, on_question=stand_in.on_question)
    await start_messaging()
    await _stand("akao", "家/厨房", "等水开", _at(21))

    seen = await _look(in_a_moment)

    assert seen == _ANSWERED
    (question,) = stand_in.questions
    assert question.sender == "赤尾"
    assert "家/厨房" in question.body and "等水开" in question.body, question.body
    assert stand_in.got == [], "提问进了 world 的普通收件箱"


@pytest.mark.integration
async def test_through_messaging_with_no_world_she_hears_nobody_answered(
    broker, messaging_db, moment_db, in_a_moment  # noqa: F811
):
    """world 没在跑（这条泳道上从没开过它的收件箱）：问题送不出去，她立刻得知没人回答。"""
    from app.messaging.lifecycle import start_messaging

    await start_messaging()
    await _stand("akao", "家/客厅", "看书", _at(21))

    async with asyncio.timeout(10):
        _nobody_answered(await _look(in_a_moment))


@pytest.mark.integration
async def test_through_messaging_a_world_that_does_not_answer_in_time_is_nobody_answering(
    broker, messaging_db, moment_db, in_a_moment, monkeypatch  # noqa: F811
):
    """world 收下了问题却迟迟不答（停在半路、或者答得太慢）：到她的截止时刻，她得知没人回答。"""
    from app.messaging.lifecycle import start_messaging
    from app.messaging.receiving import inbox

    monkeypatch.setattr(moment_mod, "LOOK_AROUND_ANSWER_SECONDS", 1.0)
    held: list[str] = []
    # 她不等了之后才放它答：这时的回答没人接。放它走也让收尾时停止接收不必等它。
    let_go = asyncio.Event()

    async def answers_too_late(question) -> str:
        held.append(question.body)
        await let_go.wait()
        return "来晚了的回答"

    async def ignore(message) -> None:
        return None

    inbox(WORLD, on_message=ignore, on_question=answers_too_late)
    await start_messaging()
    await _stand("akao", "家/客厅", "看书", _at(21))

    try:
        async with asyncio.timeout(15):
            _nobody_answered(await _look(in_a_moment))
    finally:
        let_go.set()
    assert held, "前提不成立：问题没到 world 那边"
