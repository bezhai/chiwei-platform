"""world 收件箱这个唤醒来源（``world-inbox``）：一轮开始前先问通信机制哪些处理完的消息已经记下了
成功，问完才不再让新到的投递并进这一轮，然后读还没经过一轮的消息；这一轮的【现在】在一轮开始时读。

``test_rounds.py`` 钉住的是一次投递从收下到处理完的整条路；这里钉住的是一轮开始前那一小段的边界：

* 问通信机制那一步卡住的时候到的投递，并进正要开始的这一轮，和它共享成功或失败；
* 那一步卡住的时候带头的投递被取消了（进程在停、超时），跟着等的投递失败，不会一直挂着；
* 那一步出错或者等不到回音，记录留着，这一轮照常跑；等它的那段时间里到的投递一样并进这一轮；
* 那一步跨过了整分钟，这一轮眼前的【现在】是问完之后的时刻。

后半部分直接调这个来源的 ``due``：它带上哪些消息、什么时候不跑、它不读钟；最后一条看 world 的一轮
排在宿主给它的轮次队列上。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from app.infra.cst_time import CST
from app.messaging import receiving
from app.messaging.message import Kind, new_message
from app.world import main_agent, pending
from app.world.agents import when
from app.world.rounds import RoundFailed
from tests.messaging.helpers import eventually

from .conftest import LANE, self_message, sets_wake


def _from(sender: str, body: str, *, wakes: bool = True):
    return new_message(
        sender=sender, recipient="world", body=body, kind=Kind.MESSAGE, wakes_recipient=wakes
    )


def _round_input(world, run: int = -1) -> str:
    return world.runner.runs[run][-1].content


async def _soon(awaitable):
    """该马上有结果的等待：卡住就是出了错，不让它把整个测试挂住。"""
    return await asyncio.wait_for(awaitable, timeout=5)


async def _arrived(*messages) -> None:
    """等这几条都收进了收件箱（还没经过一轮的那些里有它们）。"""
    wanted = {m.message_id for m in messages}
    await eventually(lambda: wanted <= {m.message_id for m in pending.read()}, timeout=5)


def _handled_records(volume) -> dict:
    path = volume / LANE / "pending.json"
    return json.loads(path.read_text(encoding="utf-8"))["handled"] if path.exists() else {}


class SlowAsking:
    """问通信机制"哪些处理完的消息已经记下了成功"那一步：卡住，直到放行。"""

    def __init__(self) -> None:
        self.asking = asyncio.Event()
        self.answer = asyncio.Event()

    async def __call__(self, inbox_name, message_ids):
        self.asking.set()
        await self.answer.wait()
        return set()


async def _a_handled_record(world) -> None:
    """先跑完一轮：它的消息记成处理完，下一轮开始前就要问通信机制它的成功记下了没有。"""
    await world.deliver(_from("赤尾", "我出门了。"))
    assert pending.recorded(), "没有处理完的记录，下一轮开始前不会去问通信机制"


def _fails(error: BaseException):
    async def plan():
        raise error

    return plan


# ---------------------------------------------------------------------------
# 问通信机制那一步卡住的时候
# ---------------------------------------------------------------------------


async def test_a_delivery_arriving_while_the_round_asks_messaging_joins_it_and_fails_with_it(
    world, monkeypatch
):
    """一轮开始前问通信机制那一步卡住了，这时到的投递并进这一轮：只跑一轮，两条一起带上；这一轮
    失败，并进来的那次投递也算处理失败，失败的文字说的是带着它的那一轮。"""
    await _a_handled_record(world)
    slow = SlowAsking()
    monkeypatch.setattr(receiving, "succeeded_message_ids", slow)
    world.runner.plan = _fails(RuntimeError("模型报错"))
    leader, joining = _from("千凪", "我在做饭。"), _from("绫奈", "我在看书。")

    leading = asyncio.create_task(world.deliver(leader))
    await _soon(slow.asking.wait())
    following = asyncio.create_task(world.deliver(joining))
    await _arrived(joining)
    slow.answer.set()
    outcomes = await asyncio.gather(leading, following, return_exceptions=True)

    assert isinstance(outcomes[0], RuntimeError)
    assert isinstance(outcomes[1], RoundFailed)
    assert str(outcomes[1]) == (
        "the round that took this message did not finish: RuntimeError: 模型报错"
    )
    assert isinstance(outcomes[1].__cause__, RuntimeError)
    assert len(world.runner.runs) == 2, "并进来的那次投递又自己起了一轮"
    assert "我在做饭。" in _round_input(world) and "我在看书。" in _round_input(world)


async def test_a_delivery_left_waiting_while_the_round_asks_messaging_fails_when_its_leader_goes(
    world, monkeypatch
):
    """问通信机制那一步卡住时，带头的那次投递被取消了（进程在停、超时）：并进来的那次投递算处理
    失败，不会一直挂着等一轮没人会跑的；消息都还在，之后的一轮照样带上。"""
    await _a_handled_record(world)
    slow = SlowAsking()
    asks = receiving.succeeded_message_ids
    monkeypatch.setattr(receiving, "succeeded_message_ids", slow)
    leader, joining = _from("千凪", "我在做饭。"), _from("绫奈", "我在看书。")

    leading = asyncio.create_task(world.deliver(leader))
    await _soon(slow.asking.wait())
    following = asyncio.create_task(world.deliver(joining))
    await _arrived(joining)
    leading.cancel()

    with pytest.raises(RoundFailed) as failed:
        await _soon(following)
    assert str(failed.value) == "the round that took this message did not finish: CancelledError: "
    with pytest.raises(asyncio.CancelledError):
        await leading
    assert len(world.runner.runs) == 1

    monkeypatch.setattr(receiving, "succeeded_message_ids", asks)
    await world.deliver(joining)
    assert len(world.runner.runs) == 2
    assert "我在做饭。" in _round_input(world) and "我在看书。" in _round_input(world)


async def test_when_messaging_does_not_answer_a_delivery_arriving_meanwhile_joins_the_round(
    world, volume, monkeypatch
):
    """问通信机制那一步一直没有回音：等到时限按"这次没查到"处理，记录留着，这一轮照常跑；等的
    那段时间里到的投递并进这一轮，一起处理完。"""
    monkeypatch.setattr("app.world.rounds.SETTLED_QUERY_TIMEOUT", timedelta(milliseconds=300))
    first = _from("赤尾", "我出门了。")
    await world.deliver(first)
    asking, joined = asyncio.Event(), asyncio.Event()
    asked: list[list[str]] = []

    async def never_answers(inbox_name, message_ids):
        """一直没有回音，只有时限能结束这一步。时限取消它之后，等测试看到那次投递并进来了才真的
        停下：测试机上收下那次投递慢、时限先到了，这一步也还没结束，那次投递仍然是问的时候到的。"""
        asked.append(message_ids)
        asking.set()
        try:
            await asyncio.Event().wait()
        finally:
            await joined.wait()

    monkeypatch.setattr(receiving, "succeeded_message_ids", never_answers)
    leader, joining = _from("千凪", "我在做饭。"), _from("绫奈", "我在看书。")

    leading = asyncio.create_task(world.deliver(leader))
    await _soon(asking.wait())
    following = asyncio.create_task(world.deliver(joining))
    # 记进还没经过一轮的消息到并进这一批之间没有 await：看到它到了，它就已经并进来了。
    await _arrived(joining)
    joined.set()

    assert await _soon(asyncio.gather(leading, following)) == [None, None]
    assert len(asked) == 1, "并进来的那次投递自己排了一次轮次，没有并进这一轮"
    assert len(world.runner.runs) == 2
    assert "我在做饭。" in _round_input(world) and "我在看书。" in _round_input(world)
    assert first.message_id in _handled_records(volume)


async def test_when_asking_messaging_fails_the_records_stay_and_the_round_runs(
    world, volume, monkeypatch
):
    async def database_down(inbox_name, message_ids):
        raise ConnectionError("库连不上")

    first = _from("赤尾", "我出门了。")
    await world.deliver(first)
    monkeypatch.setattr(receiving, "succeeded_message_ids", database_down)

    await _soon(world.deliver(_from("千凪", "我在做饭。")))

    assert len(world.runner.runs) == 2
    assert first.message_id in _handled_records(volume)


async def test_now_in_the_round_is_read_after_asking_messaging(world, monkeypatch):
    """问通信机制那一步跨过了整分钟：这一轮眼前的【现在】是问完、一轮开始那一刻，不是投递到达
    那一刻。"""
    await _a_handled_record(world)
    slow = SlowAsking()
    monkeypatch.setattr(receiving, "succeeded_message_ids", slow)
    clock = [datetime(2026, 7, 25, 10, 59, 58, tzinfo=CST)]
    monkeypatch.setattr("app.world.rounds.now_cst", lambda: clock[0])
    world.runner.plan = sets_wake()

    leading = asyncio.create_task(world.deliver(_from("千凪", "我在做饭。")))
    await _soon(slow.asking.wait())
    clock[0] = datetime(2026, 7, 25, 11, 0, 3, tzinfo=CST)
    slow.answer.set()
    await _soon(leading)

    assert _round_input(world).startswith(f"【现在】{when(clock[0])}\n")


# ---------------------------------------------------------------------------
# 这个来源的 due：带上哪些消息、什么时候不跑；它不读钟
# ---------------------------------------------------------------------------


def _no_clock() -> datetime:
    raise AssertionError("world 收件箱的 due 读了钟：这一轮的时刻在一轮开始时读")


def _source():
    """world 收件箱的唤醒来源，跟 world 插件建的一样（一个新的 ``Rounds`` 就是一个新进程）。"""
    from app.host import Turns
    from app.world.rounds import Rounds

    async def never_runs(due, *, round_id, now):  # pragma: no cover - due 不跑一轮
        raise AssertionError

    turns = Turns("world", "round", hold_limit=None, clock=_no_clock)
    rounds = Rounds(never_runs, round_timeout=main_agent.ROUND_TIMEOUT, turns=turns)
    return rounds.source


def _batch(message):
    """这条消息的那一次投递带来的一批。"""
    from app.host import Batch, Signal

    return Batch(Signal(at=datetime(2026, 7, 25, 11, 0, tzinfo=CST), key=message.message_id))


@pytest.fixture
def asked(monkeypatch) -> set[str]:
    """通信机制记下了成功的消息 id；问它的时候照这个回答。"""
    succeeded: set[str] = set()

    async def succeeded_message_ids(inbox_name, message_ids):
        return {m for m in message_ids if m in succeeded}

    monkeypatch.setattr(receiving, "succeeded_message_ids", succeeded_message_ids)
    return succeeded


async def test_the_round_takes_every_waiting_message_in_the_order_they_came(volume, asked):
    quiet, waking = _from("绫奈", "我在看书。", wakes=False), _from("赤尾", "我出门了。")
    pending.add(quiet)
    pending.add(waking)

    due = await _source().due("world", _batch(waking), _no_clock)

    assert due is not None
    assert [m.message_id for m in due.messages] == [quiet.message_id, waking.message_id]
    assert waking.message_id in due.reason


async def test_no_round_when_none_of_the_batch_is_still_waiting(volume, asked):
    """这一批投递带来的消息都已经不在还没经过一轮的那些里了（前一轮把它处理完了），这一轮不跑：
    别的还在等的消息，等它们自己的投递。"""
    quiet, handled = _from("绫奈", "我在看书。", wakes=False), _from("赤尾", "我出门了。")
    pending.add(quiet)

    assert await _source().due("world", _batch(handled), _no_clock) is None
    assert [m.message_id for m in pending.read()] == [quiet.message_id]


async def test_a_replaced_wake_is_left_out_and_starts_no_round(volume, asked):
    """它给自己排的这次醒来已经被后来定的时刻取代了（状态里的最新唤醒不是它）：拿掉，不跑。"""
    replaced = self_message("wake-old")
    pending.add(replaced)

    assert await _source().due("world", _batch(replaced), _no_clock) is None
    assert pending.read() == []


async def test_messages_whose_success_messaging_recorded_are_forgotten_first(volume, asked):
    earlier = _from("赤尾", "我出门了。")
    pending.add(earlier)
    pending.handled([earlier])
    asked.add(earlier.message_id)
    waking = _from("千凪", "我在做饭。")
    pending.add(waking)

    assert await _source().due("world", _batch(waking), _no_clock) is not None
    assert not pending.recorded()


async def test_when_messaging_cannot_be_asked_the_records_stay_and_the_round_is_due(
    volume, monkeypatch
):
    earlier = _from("赤尾", "我出门了。")
    pending.add(earlier)
    pending.handled([earlier])

    async def database_down(inbox_name, message_ids):
        raise ConnectionError("库连不上")

    monkeypatch.setattr(receiving, "succeeded_message_ids", database_down)
    waking = _from("千凪", "我在做饭。")
    pending.add(waking)

    due = await _source().due("world", _batch(waking), _no_clock)

    assert due is not None and [m.message_id for m in due.messages] == [waking.message_id]
    assert earlier.message_id in _handled_records(volume)


async def test_world_takes_its_rounds_on_the_queue_the_host_gave_it(app_host):
    """world 的一轮排在宿主给它的轮次队列上（``ctx.turns``）：参与者 ``world``，没有占用上限（一轮
    的时限在一轮里管）。宿主每次起来是一条新的队列，停了再起不去等上一次起来时没跑完的那一轮。"""
    host = await app_host("world")

    queues = [r for r in host.registered() if r.kind == "turns"]

    assert [(q.plugin, q.name, q.detail["turns"].hold_limit) for q in queues] == [
        ("world", "world/round", None)
    ]
    assert [r for r in host.registered() if r.kind == "wake"] == [], (
        "world 的一轮由投递叫醒，不由钟叫醒"
    )
