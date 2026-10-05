"""她做的事发出去：给 world 的一轮一条汇总，对姐妹说的话直接给那位姐妹。

汇总从她存下的经历里取，不从内存里取：一段经历变成要发的消息（id 和正文定死）和"讲到哪了"
在同一个事务里落地，然后逐条发，发的结果单独记下。没确认发出去的下一次原样再发；对方没开
收件箱是一个确定的结果，不再发。这里逐一走一遍。
"""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.data import session as session_mod
from app.living import outgoing as outgoing_mod
from app.living.happening import record_happening
from app.living.moment import act, keep_in_mind, move_to, run_moment, say, switch_to
from app.living.outgoing import OutgoingResult, OutgoingUpTo, send_what_she_did
from app.living.records import (
    KIND_ACT,
    KIND_SPEECH,
    MEDIUM_GROUP_CHAT,
    MEDIUM_PHONE,
)
from app.living.whereabouts import note_whereabouts
from tests.living.conftest import RESIDENT_NAMES, clock_at, model_facing_text
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


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


class _Died(BaseException):
    """进程在这一刻没了（部署、OOM）：不是一个能被接住的错误，后面的代码一行都不会跑。"""


async def _stand(persona: str, place: str, doing: str, at: dt.datetime) -> None:
    await note_whereabouts(
        lane=LANE,
        persona_id=persona,
        moment_id=f"before:{at.isoformat(timespec='minutes')}",
        place=place,
        doing=doing,
        noted_at=at,
    )


async def _tell(persona: str = "akao") -> None:
    await send_what_she_did(lane=LANE, persona_id=persona)


async def _within(seconds: float, step) -> None:
    """``step`` 要在 ``seconds`` 秒内做完。一次发送挂住时，挂住的应该是那一次，不是这条用例。"""
    async with asyncio.timeout(seconds):
        await step


async def _until(predicate, *, seconds: float = 5.0) -> None:
    """等到 ``predicate()`` 为真。"""
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


async def _do(hand, args: dict) -> str:
    """在这一轮里用一只手；它没做成就当场红，不然后面验的是一件没发生的事。"""
    done = await hand.invoke(args)
    assert isinstance(done, str), f"{hand.name} 没做成：{done!r}"
    return done


@pytest.fixture
async def started(moment_db, post):  # noqa: F811 — 形参名就是 fixture 名
    """赤尾已经在客厅看书，而且已经开始往外讲（她开始讲之前的经历不补发，见最后一节）。"""
    await _stand("akao", "家/客厅", "看书", _at(21, 0))
    await _tell()
    assert post.sent == []
    return post


async def _results() -> list[OutgoingResult]:
    async with session_mod.get_session() as s:
        rows = (
            await s.execute(
                text(f"SELECT * FROM {outgoing_mod._RESULT_TABLE} WHERE lane = :l"),
                {"l": LANE},
            )
        ).mappings().all()
    return [OutgoingResult(**{k: r[k] for k in OutgoingResult.model_fields}) for r in rows]


# ---------------------------------------------------------------------------
# 给 world 的汇总：她当面做的事，按先后，用她自己的话，最后是她在哪、在做什么
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_world_hears_what_she_did_in_order_and_where_she_ended_up(started, in_a_moment):
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(switch_to, {"doing": "煮乌冬", "place": "家/厨房", "because": "饿了"})
        await _do(act, {"what": "把锅放上灶"})
        await _do(say, {"what": "谁要乌冬？", "to": []})
        await _do(move_to, {"place": "家/客厅"})
        await _do(say, {"what": "许阿姨，进来坐。", "to": ["许阿姨"]})

    await _tell()

    (sent,) = post.sent
    assert (sent.sender, sent.recipient) == ("赤尾", "world")
    assert sent.body == (
        "我做了这些（按先后）：\n"
        "- 21:30 CST 改做 煮乌冬，在 家/厨房\n"
        "- 21:30 CST 把锅放上灶\n"
        "- 21:30 CST 说：「谁要乌冬？」\n"
        "- 21:30 CST 挪到 家/客厅\n"
        "- 21:30 CST 对 许阿姨 说：「许阿姨，进来坐。」\n"
        "做完这些，我在 家/客厅，正在 煮乌冬。"
    )


@pytest.mark.integration
async def test_each_stretch_of_what_she_did_is_told_once(started, in_a_moment):
    """讲过的不再讲：下一次只带上一次之后做的事。"""
    post = started
    async with in_a_moment("akao", now=_at(21, 30), moment_id="m1"):
        await _do(act, {"what": "把锅放上灶"})
    await _tell()
    async with in_a_moment("akao", now=_at(21, 40), moment_id="m2"):
        await _do(act, {"what": "关了火"})
    await _tell()
    await _tell()

    assert [s.body for s in post.sent] == [
        "我做了这些（按先后）：\n- 21:30 CST 把锅放上灶\n做完这些，我在 家/客厅，正在 看书。",
        "我做了这些（按先后）：\n- 21:40 CST 关了火\n做完这些，我在 家/客厅，正在 看书。",
    ]
    assert len({s.message_id for s in post.sent}) == 2


@pytest.mark.integration
async def test_nothing_done_in_the_world_sends_nothing(started, in_a_moment):
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(keep_in_mind, {"still_on_my_mind": ["周末去祭典"]})

    await _tell()

    assert post.sent == []


@pytest.mark.integration
async def test_phone_sends_and_takebacks_stay_out_of_what_world_hears(started):
    """手机上发的、群里说的、撤回，跟当面做的事记在同一份经历里，但隔着设备，不进汇总。

    三条的写法照 :mod:`app.living.mouth` 和 :mod:`app.living.takeback` 记经历的样子。"""
    post = started
    for happening_id, kind, medium, content in (
        ("mouth:dm", KIND_SPEECH, MEDIUM_PHONE, "到家了吗"),
        ("mouth:group", KIND_SPEECH, MEDIUM_GROUP_CHAT, "今晚谁做饭"),
        ("takeback:dm", KIND_ACT, MEDIUM_PHONE, "去撤回自己说过的那句「到家了吗」"),
    ):
        await record_happening(
            lane=LANE,
            happening_id=happening_id,
            actor="akao",
            kind=kind,
            content=content,
            occurred_at=_at(21, 30),
            medium=medium,
            channel_id="conv-1",
        )

    await _tell()
    assert post.sent == [], "手机上的事进了 world 的汇总"

    await record_happening(
        lane=LANE,
        happening_id="moment:act",
        actor="akao",
        kind=KIND_ACT,
        content="把手机扣在桌上",
        occurred_at=_at(21, 31),
    )
    await _tell()

    (sent,) = post.sent
    assert sent.body == (
        "我做了这些（按先后）：\n- 21:31 CST 把手机扣在桌上\n做完这些，我在 家/客厅，正在 看书。"
    )


# ---------------------------------------------------------------------------
# 对姐妹说的话：直接给被说的那位，按名字认
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_words_only_for_her_sisters_go_to_each_of_them_and_not_to_world(
    started, in_a_moment
):
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(say, {"what": "抹茶煮多了，要不要？", "to": ["绫奈", "千凪"]})

    await _tell()

    assert [(s.sender, s.recipient, s.body) for s in post.sent] == [
        ("赤尾", "绫奈", "当面对你和 千凪 说：「抹茶煮多了，要不要？」"),
        ("赤尾", "千凪", "当面对你和 绫奈 说：「抹茶煮多了，要不要？」"),
    ]


@pytest.mark.integration
async def test_words_for_a_sister_and_someone_else_go_to_both(started, in_a_moment):
    """姐妹收到一条直达的；还有别人在听，所以这句也进 world 的汇总，谁会察觉由 world 判断。"""
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(say, {"what": "许阿姨来了，开下门。", "to": ["绫奈", "许阿姨"]})

    await _tell()

    assert [(s.recipient, s.body) for s in post.sent] == [
        ("绫奈", "当面对你和 许阿姨 说：「许阿姨来了，开下门。」"),
        (
            "world",
            "我做了这些（按先后）：\n"
            "- 21:30 CST 对 绫奈、许阿姨 说：「许阿姨来了，开下门。」\n"
            "做完这些，我在 家/客厅，正在 看书。",
        ),
    ]


@pytest.mark.integration
async def test_a_name_that_is_not_a_sisters_is_someone_in_the_world(started, in_a_moment):
    """姐妹按世界里的名字认。``ayana`` 是 life 内部的 id，不是谁在世界里的名字。"""
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(say, {"what": "你好。", "to": ["ayana"]})

    await _tell()

    (sent,) = post.sent
    assert sent.recipient == "world"
    assert "对 ayana 说：「你好。」" in sent.body


@pytest.mark.integration
@pytest.mark.parametrize("unconfirmed", ["fails", "hangs"])
async def test_what_she_said_to_one_sister_reaches_her_in_the_order_she_said_it(
    started, in_a_moment, monkeypatch, unconfirmed
):
    """同一位姐妹的两句，前一句没确认（出错，或者一直没结果），后一句等它：前一句可能已经到了，
    也可能还没到，先发后一句就可能倒过来。别的姐妹不用等。"""
    post = started
    monkeypatch.setattr(outgoing_mod, "SEND_SECONDS", 0.2)
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(say, {"what": "饭好了。", "to": ["绫奈"]})
        await _do(say, {"what": "你也来。", "to": ["千凪"]})
        await _do(say, {"what": "快下来。", "to": ["绫奈"]})
    (post.failing if unconfirmed == "fails" else post.hanging).add("绫奈")

    await _within(5, _tell())
    assert [(s.recipient, s.body) for s in post.sent] == [
        ("绫奈", "当面对你说：「饭好了。」"),
        ("千凪", "当面对你说：「你也来。」"),
    ]

    post.failing.clear()
    post.hanging.clear()
    await _within(5, _tell())
    assert [(s.recipient, s.body) for s in post.sent[2:]] == [
        ("绫奈", "当面对你说：「饭好了。」"),
        ("绫奈", "当面对你说：「快下来。」"),
    ]


# ---------------------------------------------------------------------------
# 不丢也不重复：没确认发出去的原样再发，id 和正文都不变
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_send_that_was_not_confirmed_goes_again_with_the_same_id_and_body(
    started, in_a_moment
):
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(act, {"what": "把锅放上灶"})
    post.failing.add("world")

    await _tell()
    post.failing.clear()
    await _tell()
    await _tell()

    first, again = post.sent
    assert (again.message_id, again.body) == (first.message_id, first.body)
    assert [r.delivered for r in await _results()] == [True]


@pytest.mark.integration
async def test_a_sister_gets_her_words_with_the_time_she_said_them_on_every_send(
    started, in_a_moment
):
    """对方按消息上的时间排。补发时给发出那一刻，一句早话就排到别人后说的话后面去了。"""
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(say, {"what": "饭好了。", "to": ["绫奈"]})
    post.failing.add("绫奈")

    await _tell()
    post.failing.clear()
    await _tell()

    assert [(s.recipient, s.time) for s in post.sent] == [
        ("绫奈", _at(21, 30)),
        ("绫奈", _at(21, 30)),
    ]


@pytest.mark.integration
async def test_world_gets_a_stretch_with_the_time_it_ended_on_every_send(
    started, in_a_moment
):
    """给 world 的那条的时间是这一段里最晚的那一刻，跟正文一样补发时一字不变。"""
    post = started
    async with in_a_moment("akao", now=_at(21, 30), moment_id="m1"):
        await _do(act, {"what": "把锅放上灶"})
    async with in_a_moment("akao", now=_at(21, 40), moment_id="m2"):
        await _do(act, {"what": "关了火"})
    post.failing.add("world")

    await _tell()
    post.failing.clear()
    await _tell()

    first, again = post.sent
    assert first.time == again.time == _at(21, 40)
    assert (again.message_id, again.body) == (first.message_id, first.body)


@pytest.mark.integration
@pytest.mark.parametrize("handed_over", [False, True], ids=["before-send", "after-send"])
async def test_a_process_that_dies_while_sending_leaves_it_to_be_sent_again(
    started, in_a_moment, monkeypatch, handed_over
):
    """发之前、或者已经交给通信机制而结果还没记下时进程没了：下一次照原样再发一遍，
    对方按 id 去重。"""
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(say, {"what": "饭好了。", "to": ["绫奈"]})

    async def dies(*, sender, recipient, body, message_id=None, time=None):
        if handed_over:
            await post.send(
                sender=sender, recipient=recipient, body=body, message_id=message_id,
                time=time,
            )
        raise _Died()

    monkeypatch.setattr(outgoing_mod, "send", dies)
    with pytest.raises(_Died):
        await _tell()
    monkeypatch.setattr(outgoing_mod, "send", post.send)
    await _tell()

    composed = await outgoing_mod.unsent(lane=LANE, persona_id="akao")
    assert composed == []
    sends = [(s.recipient, s.message_id, s.body) for s in post.sent]
    expected = ("绫奈", sends[-1][1], "当面对你说：「饭好了。」")
    assert sends == ([expected, expected] if handed_over else [expected])


@pytest.mark.integration
async def test_a_process_that_dies_while_composing_composes_the_same_messages_again(
    started, in_a_moment, monkeypatch
):
    """要发的消息和"讲到哪了"在同一个事务里：写到一半进程没了，两样都不留；下一次从同一段
    经历重新生成，id 和正文跟没落地的那一次一样。"""
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(say, {"what": "许阿姨来了，开下门。", "to": ["绫奈", "许阿姨"]})

    real = outgoing_mod.insert_append
    attempted: list = []

    async def dies_at_the_mark(row, **kwargs):
        if isinstance(row, OutgoingUpTo):
            raise _Died()
        await real(row, **kwargs)
        attempted.append(row)

    monkeypatch.setattr(outgoing_mod, "insert_append", dies_at_the_mark)
    with pytest.raises(_Died):
        await _tell()
    monkeypatch.setattr(outgoing_mod, "insert_append", real)
    assert len(attempted) == 2, "前提没造出来：两条消息要在进程没了之前写进那个事务"
    assert post.sent == []
    assert await outgoing_mod.unsent(lane=LANE, persona_id="akao") == [], (
        "那个事务没落地，可它写下的消息留下了"
    )

    await _tell()

    assert [(s.message_id, s.recipient, s.body) for s in post.sent] == [
        (m.message_id, m.recipient, m.body) for m in attempted
    ]


@pytest.mark.integration
async def test_one_sister_failing_does_not_hold_back_the_other(started, in_a_moment):
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(say, {"what": "抹茶煮多了，要不要？", "to": ["绫奈", "千凪"]})
    post.failing.add("千凪")

    await _tell()
    post.failing.clear()
    await _tell()

    assert [(s.recipient, s.body) for s in post.sent] == [
        ("绫奈", "当面对你和 千凪 说：「抹茶煮多了，要不要？」"),
        ("千凪", "当面对你和 绫奈 说：「抹茶煮多了，要不要？」"),
        ("千凪", "当面对你和 绫奈 说：「抹茶煮多了，要不要？」"),
    ]
    assert post.sent[1].message_id == post.sent[2].message_id


@pytest.mark.integration
async def test_a_send_that_hangs_is_given_up_on_and_her_other_recipients_still_hear_her(
    started, in_a_moment, monkeypatch
):
    """发给绫奈的那一次一直不返回：等到上限就不等了，结果未知，跟没确认一样留着下一次原样再发；
    千凪和 world 这一次照常发出去。"""
    post = started
    monkeypatch.setattr(outgoing_mod, "SEND_SECONDS", 0.2)
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(say, {"what": "抹茶煮多了，要不要？", "to": ["绫奈", "千凪"]})
        await _do(act, {"what": "把锅端上桌"})
    post.hanging.add("绫奈")

    await _within(5, _tell())

    assert [s.recipient for s in post.sent] == ["绫奈", "千凪", "world"]
    still = await outgoing_mod.unsent(lane=LANE, persona_id="akao")
    assert [m.recipient for m in still] == ["绫奈"]

    post.hanging.clear()
    await _within(5, _tell())

    first, *_, again = post.sent
    assert again.recipient == "绫奈"
    assert (again.message_id, again.body) == (first.message_id, first.body)
    assert await outgoing_mod.unsent(lane=LANE, persona_id="akao") == []


@pytest.mark.integration
async def test_no_inbox_on_the_other_side_is_final(started, in_a_moment):
    """对方没开收件箱是一个确定的结果：记下来，不再一轮一轮地发。"""
    post = started
    async with in_a_moment("akao", now=_at(21, 30)):
        await _do(act, {"what": "把锅放上灶"})
    post.no_inbox.add("world")

    await _tell()
    await _tell()

    assert len(post.sent) == 1
    ((delivered, reason),) = [(r.delivered, r.reason) for r in await _results()]
    assert (delivered, reason) == (False, "对方没有开设收件箱")


# ---------------------------------------------------------------------------
# 从哪开始讲
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_she_did_before_she_started_telling_is_not_sent(
    moment_db, post, in_a_moment  # noqa: F811 — 形参名就是 fixture 名
):
    """开始往外讲之前的经历不补发：那时候还没有人听，现在补成一条消息，world 会当成刚发生的事。"""
    await _stand("akao", "家/客厅", "看书", _at(20, 0))
    async with in_a_moment("akao", now=_at(20, 30), moment_id="before"):
        await _do(say, {"what": "早就说过的话。", "to": ["绫奈"]})
        await _do(act, {"what": "早就做过的事"})

    await _tell()
    assert post.sent == []

    async with in_a_moment("akao", now=_at(21, 30), moment_id="after"):
        await _do(act, {"what": "把锅放上灶"})
    await _tell()

    (sent,) = post.sent
    assert sent.body == (
        "我做了这些（按先后）：\n- 21:30 CST 把锅放上灶\n做完这些，我在 家/客厅，正在 看书。"
    )


# ---------------------------------------------------------------------------
# 接在她的一轮上：结束时发这一轮的，开始前接住上一轮没发完的
# ---------------------------------------------------------------------------


async def _round(at: dt.datetime, persona: str = "akao", *, lane: str = LANE):
    return await run_moment(lane=lane, persona_id=persona, clock=clock_at(at))


@pytest.mark.integration
async def test_a_round_tells_what_she_did_when_it_ends(started, stub_moment):  # noqa: F811
    post = started
    stub_moment(
        ("switch_to", {"doing": "端菜", "place": "家/厨房", "because": "饭好了"}),
        ("say", {"what": "饭好了。", "to": ["绫奈"]}),
        ("act", {"what": "把锅端上桌"}),
        said="去吃饭",
    )

    assert await _round(_at(21, 30)) is not None

    assert [(s.sender, s.recipient, s.body) for s in post.sent] == [
        ("赤尾", "绫奈", "当面对你说：「饭好了。」"),
        (
            "赤尾",
            "world",
            "我做了这些（按先后）：\n"
            "- 21:30 CST 改做 端菜，在 家/厨房\n"
            "- 21:30 CST 把锅端上桌\n"
            "做完这些，我在 家/厨房，正在 端菜。",
        ),
    ]


@pytest.mark.integration
async def test_a_round_that_carries_on_tells_nothing(started, stub_moment):  # noqa: F811
    post = started
    stub_moment(said="继续")

    await _round(_at(21, 30))

    assert post.sent == []


@pytest.mark.integration
async def test_a_round_that_waited_behind_a_stuck_one_happens_when_it_gets_her(
    started, stub_moment, monkeypatch  # noqa: F811 — 形参名就是 fixture 名
):
    """前一轮挂住了（模型调用卡住，最长占着她 15 分钟），21:10 那一拍排在后面，21:26 才轮到。
    这一轮的『现在』是 21:26：她这一轮记下的事、发出去的消息带的都是这一刻，不是那一拍敲响的
    21:10。不然 world 收到一条 21:26 才发出的汇总，上面却盖着 21:10，而她回应的是 21:20 才
    发生的事。"""
    from app.living import moment as moment_mod
    from app.living.moment import (
        LifeMomentTick,
        latest_moment,
        life_moment_lock_key,
        life_moment_tick,
    )
    from app.living.serial import hold
    from app.living.snapshot import recent_own_happenings
    from app.living.whereabouts import current_whereabouts
    from tests.living.conftest import queued_behind

    post = started
    stub_moment(
        ("switch_to", {"doing": "端菜", "place": "家/厨房", "because": "饭好了"}),
        ("say", {"what": "饭好了。", "to": ["绫奈"]}),
        ("act", {"what": "把锅端上桌"}),
    )
    clock = [_at(21, 10)]
    monkeypatch.setattr(moment_mod, "now_cst", lambda: clock[0])
    monkeypatch.setattr(moment_mod, "living_lane", lambda: LANE)
    monkeypatch.setattr(moment_mod, "LIVING_PERSONAS", ("akao",))

    key = life_moment_lock_key(LANE, "akao")
    async with hold(key):  # 挂住的那一轮
        tick = asyncio.create_task(
            life_moment_tick(LifeMomentTick(ts=clock[0].isoformat()))
        )
        await queued_behind(key)
        clock[0] = _at(21, 26)
    await tick

    moment = await latest_moment(lane=LANE, persona_id="akao")
    assert moment is not None and moment.began_at == _at(21, 26)
    did = await recent_own_happenings(lane=LANE, persona_id="akao")
    assert [h.occurred_at for h in did] == [_at(21, 26), _at(21, 26)]
    where = await current_whereabouts(lane=LANE, persona_id="akao")
    assert where.noted_at == _at(21, 26)
    assert [(s.recipient, s.time) for s in post.sent] == [
        ("绫奈", _at(21, 26)),
        ("world", _at(21, 26)),
    ]
    assert "- 21:26 CST 把锅端上桌" in post.sent[1].body


def _fails_after_its_hands(runner, failure: BaseException | None = None, *, hangs=False):
    """这一轮的手都用完了，模型那一步才失败（或者一直不返回）。"""
    real = runner.run

    async def run(messages, **kwargs):
        reply = await real(messages, **kwargs)
        if hangs:
            await asyncio.sleep(60)
        if failure is not None:
            raise failure
        return reply

    runner.run = run


def _notes_what_was_sent_before_the_model(runner, post):
    """下一轮模型那一步被调到的时候，已经发出去了哪些。"""
    seen: list[list] = []
    real = runner.run

    async def run(messages, **kwargs):
        seen.append(list(post.sent))
        return await real(messages, **kwargs)

    runner.run = run
    return seen


@pytest.mark.integration
async def test_what_she_did_in_a_round_that_failed_is_told_before_her_next_round(
    started, stub_moment  # noqa: F811 — 形参名就是 fixture 名
):
    post = started
    runner = stub_moment(
        ("say", {"what": "饭好了。", "to": ["绫奈"]}),
        ("act", {"what": "把锅端上桌"}),
    )
    _fails_after_its_hands(runner, RuntimeError("这一轮的模型调用失败了"))
    with pytest.raises(RuntimeError, match="模型调用失败"):
        await _round(_at(21, 30))
    assert post.sent == [], "失败的那一轮走不到收尾，前提没造出来"

    seen = _notes_what_was_sent_before_the_model(stub_moment(said="继续"), post)
    await _round(_at(21, 40))

    (before_the_model,) = seen
    assert [(s.recipient, s.body) for s in before_the_model] == [
        ("绫奈", "当面对你说：「饭好了。」"),
        (
            "world",
            "我做了这些（按先后）：\n- 21:30 CST 把锅端上桌\n做完这些，我在 家/客厅，正在 看书。",
        ),
    ]
    assert post.sent == before_the_model, "下一轮把上一轮的又发了一遍"


@pytest.mark.integration
async def test_what_she_did_in_a_round_cut_off_for_taking_too_long_is_told_next_time(
    started, stub_moment, monkeypatch  # noqa: F811 — 形参名就是 fixture 名
):
    """一轮占着她的时间到顶被掐断（:data:`app.living.serial.HELD_SECONDS`），跟失败一样：做了的
    事下一轮开始前发出去。"""
    from app.living import serial as serial_mod

    post = started
    monkeypatch.setattr(serial_mod, "HELD_SECONDS", 3.0)
    runner = stub_moment(("act", {"what": "把锅端上桌"}))
    _fails_after_its_hands(runner, hangs=True)
    with pytest.raises(TimeoutError):
        await _round(_at(21, 30))
    assert post.sent == []

    monkeypatch.setattr(serial_mod, "HELD_SECONDS", 900.0)
    seen = _notes_what_was_sent_before_the_model(stub_moment(said="继续"), post)
    await _round(_at(21, 40))

    (before_the_model,) = seen
    assert [s.body for s in before_the_model] == [
        "我做了这些（按先后）：\n- 21:30 CST 把锅端上桌\n做完这些，我在 家/客厅，正在 看书。"
    ]


async def _delivered_to() -> list[str]:
    """已经确认送到的那几条，各发给了谁。"""
    by_id = {
        m.message_id: m.recipient
        for m in await _composed()
    }
    return sorted(by_id[r.message_id] for r in await _results() if r.delivered)


async def _composed() -> list:
    """她要发的消息，全部（有没有结果都算），按先后。"""
    async with session_mod.get_session() as s:
        rows = (
            await s.execute(
                text(
                    f"SELECT * FROM {outgoing_mod._MESSAGE_TABLE} "
                    f"WHERE lane = :l ORDER BY persona_id, seq"
                ),
                {"l": LANE},
            )
        ).mappings().all()
    return [
        outgoing_mod.OutgoingMessage(
            **{k: r[k] for k in outgoing_mod.OutgoingMessage.model_fields}
        )
        for r in rows
    ]


@pytest.mark.integration
async def test_a_send_that_hangs_does_not_cut_off_her_rounds(
    started, stub_moment, monkeypatch  # noqa: F811 — 形参名就是 fixture 名
):
    """发给绫奈的那一次挂住。不给一次发送封顶的话，这一轮在占用到顶时被掐断，下一轮开始前又先去
    发同一条、又挂住：她一轮一轮被掐断，千凪和 world 一直轮不到。"""
    from app.living import serial as serial_mod

    post = started
    # 不封顶的那一版红在这里：要等满占用上限。压到 5 秒，红得快一点。
    monkeypatch.setattr(serial_mod, "HELD_SECONDS", 5.0)
    monkeypatch.setattr(outgoing_mod, "SEND_SECONDS", 0.2)
    post.hanging.add("绫奈")
    stub_moment(
        ("say", {"what": "抹茶煮多了，要不要？", "to": ["绫奈", "千凪"]}),
        ("act", {"what": "把锅端上桌"}),
    )

    assert await _round(_at(21, 30)) is not None
    stub_moment(said="继续")
    assert await _round(_at(21, 40)) is not None

    assert await _delivered_to() == ["world", "千凪"]
    assert [m.recipient for m in await outgoing_mod.unsent(lane=LANE, persona_id="akao")] == [
        "绫奈"
    ]


@pytest.mark.integration
async def test_a_round_cancelled_while_a_send_is_in_flight_loses_nothing_and_doubles_nothing(
    started, stub_moment  # noqa: F811 — 形参名就是 fixture 名
):
    """这一轮在一次发送还没结果时被取消（占用到顶、部署）。那一次结果未知，没有结果的那条下一次
    原样再发；已经有结果的不再发；要发的消息不多也不少。"""
    post = started
    post.hanging.add("千凪")
    stub_moment(
        ("say", {"what": "抹茶煮多了，要不要？", "to": ["绫奈", "千凪"]}),
        ("act", {"what": "把锅端上桌"}),
    )
    first = asyncio.create_task(_round(_at(21, 30)))
    await _until(lambda: [s.recipient for s in post.sent] == ["绫奈", "千凪"])
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    post.hanging.clear()
    stub_moment(said="继续")
    await _round(_at(21, 40))

    assert [s.recipient for s in post.sent] == ["绫奈", "千凪", "千凪", "world"]
    assert (post.sent[1].message_id, post.sent[1].body) == (
        post.sent[2].message_id,
        post.sent[2].body,
    )
    assert [m.recipient for m in await _composed()] == ["绫奈", "千凪", "world"]
    assert await _delivered_to() == ["world", "千凪", "绫奈"]


async def _marks(persona: str = "akao") -> list[tuple[int, int]]:
    """她每一次"讲到哪了"，按先后。"""
    async with session_mod.get_session() as s:
        rows = (
            await s.execute(
                text(
                    f"SELECT happening_seq, whereabouts_seq FROM {outgoing_mod._UP_TO_TABLE} "
                    f"WHERE lane = :l AND persona_id = :p "
                    f"ORDER BY happening_seq, whereabouts_seq"
                ),
                {"l": LANE, "p": persona},
            )
        ).all()
    return [tuple(r) for r in rows]


async def _her_seqs(table: str, persona_column: str, persona: str = "akao") -> list[int]:
    async with session_mod.get_session() as s:
        return list(
            (
                await s.execute(
                    text(
                        f"SELECT seq FROM {table} "
                        f"WHERE lane = :l AND {persona_column} = :p ORDER BY seq"
                    ),
                    {"l": LANE, "p": persona},
                )
            ).scalars()
        )


@pytest.mark.integration
async def test_a_woken_round_that_keeps_failing_tells_each_thing_once_while_others_live_on(
    started, moment_db, stub_moment, in_a_moment, monkeypatch  # noqa: F811 — 形参名就是 fixture 名
):
    """world 的一条把她叫醒，那一轮在她做完事之后失败了两次，每一拍都以同一个身份重跑
    （:class:`app.living.nudge.NudgeBegun`）——哪怕中间又到了一条发生得更早的、按"最早没看过的
    那条"本该换它叫醒她；第三次她多做了一件事、落了地。这期间千凪一直在记自己的经历，经历的
    seq 是全泳道一条轴，两人的号交错。

    她要发的消息一条不多一条不少：那句话、每件事各进一次；讲到哪了只停在她自己的号上；重跑的
    那几次『现在』一次比一次晚，消息上的时间还是她第一次做那件事的时候。"""
    from app.living.nudge import NudgeBegun, nudge_once
    from app.living.received import receive
    from app.messaging.message import Kind, new_message
    from tests.runtime.conftest import migrate

    post = started
    await migrate(NudgeBegun, moment_db)
    monkeypatch.setenv("LANE", LANE)
    await _stand("chinagi", "家/厨房", "煮乌冬", _at(21, 0))

    async def chinagi_does(what: str, at: dt.datetime) -> None:
        async with in_a_moment("chinagi", now=at, moment_id=f"c:{what}"):
            await _do(act, {"what": what})

    rain = new_message(
        sender="world", recipient="赤尾", body="窗外下起了雨。", kind=Kind.MESSAGE,
        time=_at(21, 31),
    )
    await receive(rain)
    said = ("say", {"what": "下雨了，收衣服。", "to": ["绫奈"]})
    closed = ("act", {"what": "起身把窗关上了"})

    runner = stub_moment(said, closed)
    _fails_after_its_hands(runner, RuntimeError("这一轮在她做完事之后失败了"))
    for minute in (32, 33):
        with pytest.raises(RuntimeError, match="做完事之后失败"):
            await nudge_once(lane=LANE, persona_id="akao", clock=clock_at(_at(21, minute)))
        await chinagi_does(f"搅了搅锅（{minute}）", _at(21, minute))
        if minute == 32:
            await receive(
                new_message(
                    sender="world", recipient="赤尾", body="楼下有人按门铃。",
                    kind=Kind.MESSAGE, time=_at(21, 20),
                )
            )

    stub_moment(said, closed, ("act", {"what": "把晾的衣服收进来了"}))
    landed = await nudge_once(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 34)))
    assert landed is not None and landed.moment_id == f"nudge:inbox:{rain.message_id}"
    assert await nudge_once(lane=LANE, persona_id="akao", clock=clock_at(_at(21, 35))) is None

    hers = await _her_seqs(outgoing_mod._HAPPENING_TABLE, "actor")
    others = await _her_seqs(outgoing_mod._HAPPENING_TABLE, "actor", "chinagi")
    assert len(hers) == 3 and len(others) == 2
    assert hers[1] < others[0] < others[1] < hers[2], "前提没造出来：两人的号要交错"
    where = max(await _her_seqs(outgoing_mod._WHEREABOUTS_TABLE, "persona_id"))

    expected = [
        ("绫奈", "当面对你说：「下雨了，收衣服。」", _at(21, 32)),
        (
            "world",
            "我做了这些（按先后）：\n- 21:32 CST 起身把窗关上了\n做完这些，我在 家/客厅，正在 看书。",
            _at(21, 32),
        ),
        (
            "world",
            "我做了这些（按先后）：\n- 21:34 CST 把晾的衣服收进来了\n"
            "做完这些，我在 家/客厅，正在 看书。",
            _at(21, 34),
        ),
    ]
    assert [(m.recipient, m.body, m.message_time) for m in await _composed()] == expected
    assert [(s.recipient, s.body, s.time) for s in post.sent] == expected
    assert await _delivered_to() == ["world", "world", "绫奈"]
    assert (await _marks())[1:] == [(hers[1], where), (hers[2], where)]


def test_the_say_hand_asks_for_names_in_the_world_and_offers_none():
    """举出来的名字就是词表，她会逐字抄走；姐妹的名字在人设表里，代码里一个都不写。"""
    texts = model_facing_text(say)
    everything = "\n".join(texts.values())

    assert [n for n in (*RESIDENT_NAMES, *RESIDENT_NAMES.values()) if n in everything] == []
    assert "世界里的名字" in texts["参数 to"]


@pytest.mark.integration
async def test_through_messaging_her_sister_hears_her_words_and_world_hears_the_rest(
    broker, messaging_db, moment_db, stub_moment, monkeypatch  # noqa: F811
):
    """真的通信机制：姐妹的收件箱按人设表的名字开，world 那边用一个只记账的收件箱代替。

    姐妹那句直接到了绫奈那里，出现在她下一轮的输入里；world 收到一条汇总；记录者里发给 world
    的消息没有那句只对姐妹说的话。"""
    from app.living import participants as participants_mod
    from app.living.participants import WORLD
    from app.living.received import open_inboxes
    from app.living.records import living_lane
    from app.messaging.lifecycle import start_messaging
    from app.messaging.receiving import inbox, inboxes_at_start
    from app.messaging.record import read_record
    from tests.messaging.helpers import Inbox, eventually

    async def find_persona(persona_id: str):
        return SimpleNamespace(persona_id=persona_id, display_name=RESIDENT_NAMES[persona_id])

    monkeypatch.setattr(participants_mod, "find_persona", find_persona)
    monkeypatch.setattr(participants_mod, "_known", None)
    world = Inbox()
    inbox(WORLD, on_message=world.on_message)
    inboxes_at_start(open_inboxes)
    await start_messaging()
    lane = living_lane()

    stub_moment(
        ("switch_to", {"doing": "煮乌冬", "place": "家/厨房", "because": "饿了"}),
        ("say", {"what": "饭好了，下来吃。", "to": ["绫奈"]}),
        ("say", {"what": "谁把盐放这儿了？", "to": []}),
    )
    await _round(_at(21, 30), lane=lane)

    await eventually(lambda: len(world.got) == 1)
    (told,) = world.got
    assert (told.sender, told.body) == (
        "赤尾",
        "我做了这些（按先后）：\n"
        "- 21:30 CST 改做 煮乌冬，在 家/厨房\n"
        "- 21:30 CST 说：「谁把盐放这儿了？」\n"
        "做完这些，我在 家/厨房，正在 煮乌冬。",
    )
    to_world = [r["body"] for r in await read_record(participant=WORLD)]
    assert to_world and all("饭好了，下来吃。" not in body for body in to_world)

    her_turn = stub_moment(said="继续")

    async def she_has_it() -> bool:
        from app.living.received import unread_received

        return bool(await unread_received(lane=lane, persona_id="ayana"))

    await eventually(she_has_it)
    await _round(_at(21, 31), "ayana", lane=lane)

    (what_she_read, _kwargs) = her_turn.runs[-1]
    assert "赤尾：当面对你说：「饭好了，下来吃。」" in what_she_read[-1].content


@pytest.mark.integration
async def test_through_messaging_her_sister_reads_words_in_the_order_they_were_said(
    broker, messaging_db, moment_db, stub_moment, in_a_moment, monkeypatch  # noqa: F811
):
    """真的通信机制：赤尾 21:30 对绫奈说的那句第一次没发出去，千凪 21:35 对她说的那句先到了，
    赤尾那句之后补发。绫奈读到的还是先赤尾、后千凪：她那边按消息上的时间排，那是说出口的时间。"""
    from app.living import participants as participants_mod
    from app.living.participants import WORLD
    from app.living.received import open_inboxes, unread_received
    from app.living.records import living_lane
    from app.messaging.lifecycle import start_messaging
    from app.messaging.message import SendFailed
    from app.messaging.receiving import inbox, inboxes_at_start
    from tests.messaging.helpers import Inbox, eventually

    async def find_persona(persona_id: str):
        return SimpleNamespace(persona_id=persona_id, display_name=RESIDENT_NAMES[persona_id])

    monkeypatch.setattr(participants_mod, "find_persona", find_persona)
    monkeypatch.setattr(participants_mod, "_known", None)
    inbox(WORLD, on_message=Inbox().on_message)
    inboxes_at_start(open_inboxes)
    await start_messaging()
    lane = living_lane()

    real_send = outgoing_mod.send
    refused: list[str] = []

    async def refuses_her_first(**kwargs):
        if kwargs["recipient"] == "绫奈" and not refused:
            refused.append(kwargs["message_id"])
            raise SendFailed("broker 没有确认", message_id=kwargs["message_id"])
        return await real_send(**kwargs)

    monkeypatch.setattr(outgoing_mod, "send", refuses_her_first)

    for persona in ("akao", "chinagi"):
        await note_whereabouts(
            lane=lane, persona_id=persona, moment_id="before", place="家/客厅",
            doing="看书", noted_at=_at(21, 0),
        )
        await send_what_she_did(lane=lane, persona_id=persona)

    async with in_a_moment("akao", lane=lane, now=_at(21, 30), moment_id="a"):
        await _do(say, {"what": "饭好了，下来吃。", "to": ["绫奈"]})
    await send_what_she_did(lane=lane, persona_id="akao")
    assert refused, "前提没造出来：赤尾那句第一次要没发出去"

    async with in_a_moment("chinagi", lane=lane, now=_at(21, 35), moment_id="c"):
        await _do(say, {"what": "我先吃了。", "to": ["绫奈"]})
    await send_what_she_did(lane=lane, persona_id="chinagi")

    async def she_has(n: int) -> bool:
        return len(await unread_received(lane=lane, persona_id="ayana")) == n

    await eventually(lambda: she_has(1))
    await send_what_she_did(lane=lane, persona_id="akao")
    await eventually(lambda: she_has(2))

    her_turn = stub_moment(said="继续")
    await _round(_at(21, 41), "ayana", lane=lane)

    (what_she_read, _kwargs) = her_turn.runs[-1]
    seen = what_she_read[-1].content
    akao_said = seen.index("赤尾：当面对你说：「饭好了，下来吃。」")
    chinagi_said = seen.index("千凪：当面对你说：「我先吃了。」")
    assert akao_said < chinagi_said, seen
