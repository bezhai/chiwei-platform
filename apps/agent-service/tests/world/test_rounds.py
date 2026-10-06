"""world 一轮处理收件箱里所有还没经过一轮的消息，一次只跑一轮。

一次投递就是调一次接线里交给通信机制的那个处理函数（``world.deliver``）；它返回就是这次投递
处理成功（通信机制确认它），抛异常就是处理失败（通信机制按它的重试再投一次，这里就是再调一次）。
"进程死了再起来"是重新执行一遍接线（:func:`restart`），私有卷上的东西还在。模型、上下文存储、
通信机制的发送换成替身（``conftest.py``）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

import pytest

from app.infra.cst_time import now_cst
from app.messaging import receiving
from app.messaging.message import Kind, new_message
from app.world import main_agent, pending, wake
from app.world.rounds import RoundFailed, Rounds
from tests.messaging.helpers import eventually

from .conftest import LANE, restart, self_message, sets_nothing, sets_wake


class Crash(BaseException):
    """进程在这一步死了：什么都接不住它。"""


def _from(sender: str, body: str, *, wakes: bool = True):
    return new_message(
        sender=sender, recipient="world", body=body, kind=Kind.MESSAGE, wakes_recipient=wakes
    )


def _round_input(world, run: int = -1) -> str:
    return world.runner.runs[run][-1].content


def _shown(round_input: str, bodies: list[str]) -> list[str]:
    """``bodies`` 里出现在这一轮眼前的那几条，按它们在眼前的先后。"""
    return sorted((b for b in bodies if b in round_input), key=round_input.index)


def in_turn(world, *plans):
    """第 n 轮按第 n 个替身跑。"""

    async def plan():
        return await plans[len(world.runner.runs) - 1]()

    return plan


def held(gate: asyncio.Event):
    """这一轮卡在模型那一段，直到放行；放行之后照常定时刻。"""

    async def plan():
        await gate.wait()
        return await sets_wake()()

    return plan


def raises(error: BaseException):
    async def plan():
        raise error

    return plan


async def _soon(awaitable):
    """该马上有结果的等待：卡住就是出了错，不让它把整个测试挂住。"""
    return await asyncio.wait_for(awaitable, timeout=5)


async def _arrived(*messages) -> None:
    """等这几条都收进了收件箱（还没经过一轮的那些里有它们）。"""
    wanted = {m.message_id for m in messages}
    await eventually(lambda: wanted <= {m.message_id for m in pending.read()}, timeout=5)


async def _deliver_one_by_one(world, messages) -> list[asyncio.Task]:
    """一条接一条投进来，每条收下了再投下一条：到达的先后是确定的。"""
    tasks = []
    for message in messages:
        tasks.append(asyncio.create_task(world.deliver(message)))
        await _arrived(message)
    return tasks


async def _a_round_is_running(world, deliver_first) -> asyncio.Task:
    return await _a_round_is_running_after(world, 0, deliver_first)


async def _a_round_is_running_after(world, runs_before: int, deliver) -> asyncio.Task:
    running = asyncio.create_task(deliver)
    await eventually(lambda: len(world.runner.runs) == runs_before + 1, timeout=5)
    return running


# ---------------------------------------------------------------------------
# 一轮进行中到的几条，下一轮一起处理
# ---------------------------------------------------------------------------


async def test_messages_arriving_while_a_round_runs_are_taken_together_by_the_next_round(world):
    gate = asyncio.Event()
    world.runner.plan = in_turn(world, held(gate), sets_wake())
    later = [_from("千凪", "我在做饭。"), _from("绫奈", "我在看书。"), _from("赤尾", "我到车站了。")]

    running = await _a_round_is_running(world, world.deliver(_from("赤尾", "我出门了。")))
    waiting = await _deliver_one_by_one(world, later)
    gate.set()
    await asyncio.gather(running, *waiting)

    # 每一次投递都处理成功了，可只跑了两轮：后到的三条各自的投递没有各起一轮。
    assert len(world.runner.runs) == 2
    second = _round_input(world)
    assert "这一轮有 3 条消息" in second
    assert _shown(second, [m.body for m in later]) == [m.body for m in later]
    assert "我出门了。" not in second
    assert pending.read() == []


# ---------------------------------------------------------------------------
# 一轮失败、进程中途退出：这一轮带着的消息一条不丢，之后的一轮全部带上，也不处理第二遍
# ---------------------------------------------------------------------------


async def test_a_failed_round_fails_every_delivery_it_took_and_the_next_round_takes_them_all(
    world,
):
    gate = asyncio.Event()
    world.runner.plan = in_turn(
        world, held(gate), raises(RuntimeError("模型报错")), sets_wake()
    )
    second, third = _from("千凪", "我在做饭。"), _from("绫奈", "我在看书。")

    running = await _a_round_is_running(world, world.deliver(_from("赤尾", "我出门了。")))
    waiting = await _deliver_one_by_one(world, [second, third])
    gate.set()
    outcomes = await asyncio.gather(running, *waiting, return_exceptions=True)

    assert outcomes[0] is None
    # 两条的投递都算处理失败（通信机制会各自重试），哪条也没有被当成处理完。
    assert all(isinstance(o, Exception) for o in outcomes[1:]), outcomes
    assert any(isinstance(o, RoundFailed) for o in outcomes[1:])

    # 先到的是后一条的重试：这一轮把前一条也带上，虽然前一条自己的重试还没来。
    await world.deliver(third)
    assert len(world.runner.runs) == 3
    assert _shown(_round_input(world), ["我在做饭。", "我在看书。"]) == ["我在做饭。", "我在看书。"]

    # 前一条的重试到了：它已经被处理完，不再起一轮。
    await world.deliver(second)
    assert len(world.runner.runs) == 3


async def test_the_messages_of_a_round_the_process_died_in_come_back_in_the_next_process(world):
    gate = asyncio.Event()
    world.runner.plan = in_turn(world, held(gate), raises(Crash()), sets_wake())
    second, third = _from("千凪", "我在做饭。"), _from("绫奈", "我在看书。", wakes=False)

    running = await _a_round_is_running(world, world.deliver(_from("赤尾", "我出门了。")))
    waiting = await _deliver_one_by_one(world, [second])
    await _soon(world.deliver(third))  # 不叫醒的：收下就算送达，不等正在跑的那一轮
    gate.set()
    outcomes = await asyncio.gather(running, *waiting, return_exceptions=True)
    assert isinstance(outcomes[1], Crash)

    restart(world)
    await world.deliver(second)  # 没确认的那条被 broker 重投给新进程

    assert len(world.runner.runs) == 3
    assert _shown(_round_input(world), ["我在做饭。", "我在看书。"]) == ["我在做饭。", "我在看书。"]


async def test_a_round_dying_after_its_transcript_was_stored_runs_its_messages_again(
    world, monkeypatch
):
    """已知的边界，照现在的样子钉住：一轮的上下文存下了、下次醒来也排好了，记成处理完之前进程
    死了。这一轮带着的消息一条不丢，在下一个进程里再跑一遍——上下文里会有两轮看过它们。"""
    gate = asyncio.Event()
    world.runner.plan = in_turn(world, held(gate), sets_wake(), sets_wake())
    second, third = _from("千凪", "我在做饭。"), _from("绫奈", "我在看书。")
    real = pending.handled
    calls: list[int] = []

    def dies_on_the_second_round(messages):
        calls.append(1)
        if len(calls) == 2:
            raise Crash()
        real(messages)

    monkeypatch.setattr(pending, "handled", dies_on_the_second_round)

    running = await _a_round_is_running(world, world.deliver(_from("赤尾", "我出门了。")))
    waiting = await _deliver_one_by_one(world, [second, third])
    gate.set()
    outcomes = await asyncio.gather(running, *waiting, return_exceptions=True)
    assert outcomes[0] is None and isinstance(outcomes[1], Crash)
    assert len(world.committed) == 2, "那一轮的上下文已经存下了"

    restart(world)
    await world.deliver(second)  # 没确认的投递被 broker 重投给新进程

    assert len(world.runner.runs) == 3
    assert _shown(_round_input(world), ["我在做饭。", "我在看书。"]) == ["我在做饭。", "我在看书。"]
    assert len(world.committed) == 3
    assert pending.read() == []


async def test_a_delivery_left_waiting_when_the_one_ahead_of_it_is_cancelled_fails_not_hangs(
    world,
):
    """排在一轮后面的那一批，带头的那次投递在等的时候被取消了（进程在停、超时）：同一批里
    别的投递算处理失败、照常重试，不跟着变成"被取消"，消息都还在，之后的一轮照样带上。"""
    gate = asyncio.Event()
    world.runner.plan = in_turn(world, held(gate), sets_wake())
    second, third = _from("千凪", "我在做饭。"), _from("绫奈", "我在看书。")

    running = await _a_round_is_running(world, world.deliver(_from("赤尾", "我出门了。")))
    leading, following = await _deliver_one_by_one(world, [second, third])
    leading.cancel()
    with pytest.raises(RoundFailed):
        await _soon(following)
    gate.set()
    await running

    await world.deliver(third)
    assert len(world.runner.runs) == 2
    assert _shown(_round_input(world), ["我在做饭。", "我在看书。"]) == ["我在做饭。", "我在看书。"]


# ---------------------------------------------------------------------------
# 不叫醒的消息不单独起一轮；自定唤醒那一轮同样带上待处理的；过期的仍然跳过
# ---------------------------------------------------------------------------


async def test_a_message_that_does_not_wake_world_starts_no_round_and_is_seen_in_the_next(world):
    quiet = _from("绫奈", "我在客厅看书。", wakes=False)

    await _soon(world.deliver(quiet))
    assert world.runner.runs == []

    await world.deliver(_from("赤尾", "我把窗打开了。"))
    [_] = world.runner.runs
    assert _shown(_round_input(world), ["我在客厅看书。", "我把窗打开了。"]) == [
        "我在客厅看书。",
        "我把窗打开了。",
    ]


async def test_a_round_woken_by_its_own_time_takes_the_waiting_messages_along(world):
    current = await wake.set_next_wake(now_cst(), "该看看外面了。")
    world.runner.plan = in_turn(world, sets_nothing(), sets_wake())
    left_by_a_failed_round = _from("千凪", "我在做饭。")
    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(left_by_a_failed_round)
    await world.deliver(_from("绫奈", "我在客厅看书。", wakes=False))

    await world.deliver(self_message(current.message_id, "该看看外面了。"))

    assert len(world.runner.runs) == 2
    taken = _round_input(world)
    assert "这一轮有 3 条消息" in taken
    assert _shown(taken, ["我在做饭。", "我在客厅看书。", "该看看外面了。"]) == [
        "我在做饭。",
        "我在客厅看书。",
        "该看看外面了。",
    ]
    assert "你给自己排的一次醒来" in taken


async def test_a_wake_replaced_while_it_waited_is_left_out_and_starts_no_round(world):
    planned = await wake.set_next_wake(now_cst(), "到点了。")
    gate = asyncio.Event()
    world.runner.plan = in_turn(world, held(gate), sets_wake())
    its_time = self_message(planned.message_id, "到点了。")

    running = await _a_round_is_running(world, world.deliver(_from("赤尾", "我出门了。")))
    [waiting] = await _deliver_one_by_one(world, [its_time])
    gate.set()  # 正在跑的这一轮定了新的时刻，排着的那条成了旧的
    await asyncio.gather(running, waiting)

    assert len(world.runner.runs) == 1
    assert wake.is_stale_wake(its_time)
    assert pending.read() == []


async def test_a_new_wake_that_arrives_before_it_is_recorded_is_not_skipped(world, monkeypatch):
    """一轮收尾时先把新的醒来排出去、broker 确认之后才记成最新唤醒；通信机制发送之后还有记账。
    新时刻已经到了（这一轮收尾比它定的时刻还晚）时，那条醒来在"记下来"之前就可能送到。它不能
    在那一刻被判成旧消息、确认掉——那样状态里记着它，却再也没有它这条消息，world 不会再醒。"""
    early: list[asyncio.Task] = []

    async def send_at(**kw):
        world.scheduled.append(kw)
        if not early:
            due = new_message(
                sender=kw["sender"],
                recipient=kw["recipient"],
                body=kw["body"],
                kind=Kind.MESSAGE,
                time=kw["at"],
                message_id=kw["message_id"],
            )
            early.append(asyncio.create_task(world.deliver(due)))
            # broker 立刻把它交给了收件箱：让这次投递走到它能走到的地方，然后才记下来。
            await eventually(
                lambda: early[0].done() or due.message_id in {m.message_id for m in pending.read()},
                timeout=5,
            )
        return kw["message_id"]

    monkeypatch.setattr(wake, "send_at", send_at)

    await world.deliver(_from("赤尾", "我出门了。"))
    await _soon(early[0])

    assert len(world.runner.runs) == 2, "新的醒来被当成旧消息确认掉了"
    assert "你给自己排的一次醒来" in _round_input(world)


async def test_a_replaced_wake_does_not_run_a_round_for_messages_left_by_a_failed_one(world):
    """旧的自定醒来不叫醒 world：之前失败的轮里留下的消息等它们自己的重试，不借它跑一轮。"""
    replaced = await wake.set_next_wake(now_cst() + timedelta(hours=1), "第一次定的。")
    await wake.set_next_wake(now_cst() + timedelta(hours=4), "后来改的。")
    world.runner.plan = in_turn(world, sets_nothing(), sets_wake())
    left = _from("千凪", "我在做饭。")
    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(left)

    await world.deliver(self_message(replaced.message_id))
    assert len(world.runner.runs) == 1

    await world.deliver(left)  # 它自己的重试
    assert len(world.runner.runs) == 2
    assert "我在做饭。" in _round_input(world)


async def test_a_retry_that_arrives_while_its_message_is_being_handled_runs_no_further_round(
    world, monkeypatch
):
    """一条消息的重试在带着它的那一轮跑着的时候到了：它排到下一轮，下一轮开始时它已经处理完了，
    这次投递就不再起一轮——哪怕这时还有别的消息在等（不叫醒的那条等下一次真正要一轮的投递）。"""
    gate = asyncio.Event()
    world.runner.plan = in_turn(world, sets_nothing(), held(gate), sets_wake())
    left = _from("千凪", "我在做饭。")
    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(left)

    running = await _a_round_is_running_after(world, 1, world.deliver(_from("赤尾", "我出门了。")))
    assert "我在做饭。" in _round_input(world)
    added: list[str] = []
    real_add = pending.add

    def add(message):
        real_add(message)
        added.append(message.message_id)  # 收下之后不经 await 就排到了下一轮

    monkeypatch.setattr(pending, "add", add)
    retry = asyncio.create_task(world.deliver(left))
    await eventually(lambda: added == [left.message_id], timeout=5)
    quiet = _from("绫奈", "我在看书。", wakes=False)
    await _soon(world.deliver(quiet))
    gate.set()
    await _soon(asyncio.gather(running, retry))

    assert len(world.runner.runs) == 2
    assert [m.body for m in pending.read()] == ["我在看书。"]


# ---------------------------------------------------------------------------
# 一直跑不完的消息：经过的失败轮数跟通信机制给一次投递的处理次数一样多，就不再带进之后的轮
# ---------------------------------------------------------------------------


def _fails_while_it_sees(body: str):
    async def plan():
        if body in plan.world.runner.runs[-1][-1].content:
            raise RuntimeError("这一轮跑不完")
        return await sets_wake()()

    return plan


# 一段比日志截断长度长得多的正文：放弃的日志里要有整段，才能照着重做。
LONG = "跑不完的那条。" + "她在厨房把锅放上灶，" * 40


def _gave_up(caplog) -> list[dict]:
    """日志里放弃了的消息：每条 error 末尾是整条消息的 JSON。"""
    found = []
    for record in caplog.records:
        if record.levelno == logging.ERROR and "given up" in record.getMessage():
            found.append(json.loads(record.getMessage().split("message: ", 1)[1]))
    return found


async def test_a_given_up_message_is_logged_whole_and_runs_again_when_it_is_replayed(
    world, caplog
):
    plan = _fails_while_it_sees("跑不完的那条。")
    plan.world = world
    world.runner.plan = plan
    stuck = _from("赤尾", LONG)

    for _ in range(receiving.PROCESSING_RETRY.n):
        with pytest.raises(RuntimeError):
            await world.deliver(stuck)

    # 放弃了：之后的轮不带它；日志里是整条消息，照着就能重做。
    assert stuck.message_id not in {m.message_id for m in pending.read()}
    assert _gave_up(caplog) == [stuck.to_json()]
    await world.deliver(_from("绫奈", "我在看书。"))
    assert "跑不完的那条。" not in _round_input(world)

    # 死信重放（或者它还没用完的一次重试）再投进来：重新收下，下一轮带着它。
    world.runner.plan = sets_wake()
    tries = len(world.runner.runs)
    await world.deliver(stuck)
    assert len(world.runner.runs) == tries + 1
    assert LONG in _round_input(world)


async def test_a_message_given_up_alongside_another_comes_back_with_its_remaining_retry(
    world, caplog
):
    """一条每次都让一轮跑不完的消息（不叫醒的，没有投递在重试）在等，别的消息跟着它失败、被放弃。
    被放弃的那条自己的投递还会重试：重试一来就重新收下，跟着下一轮跑完。不叫醒的那条没有死信，
    日志里的整条消息是它唯一能找回来的地方。"""
    current = await wake.set_next_wake(now_cst(), "到点了。")
    plan = _fails_while_it_sees("跑不完的那条。")
    plan.world = world
    world.runner.plan = plan
    poison = _from("绫奈", LONG, wakes=False)
    caught = _from("千凪", "我在做饭。")
    its_time = self_message(current.message_id, "到点了。")

    await world.deliver(poison)
    with pytest.raises(RuntimeError):
        await world.deliver(caught)
    for _ in range(receiving.PROCESSING_RETRY.n * 2):
        if not {m.message_id for m in pending.read()} & {poison.message_id, caught.message_id}:
            break
        with pytest.raises(RuntimeError):
            await world.deliver(its_time)  # 自定醒来不限次数重试，每次都带着它们失败
    else:
        pytest.fail("失败了这么多轮，还是没有放弃它们")

    assert sorted(m["message_id"] for m in _gave_up(caplog)) == sorted(
        [poison.message_id, caught.message_id]
    )
    assert poison.to_json() in _gave_up(caplog)

    await world.deliver(its_time)
    await world.deliver(caught)  # 它还没用完的一次重试
    assert "我在做饭。" in _round_input(world) and "跑不完的那条。" not in _round_input(world)
    assert pending.read() == []


async def test_a_retry_queued_while_its_message_is_given_up_fails_and_is_not_acknowledged(
    world, monkeypatch
):
    """这条消息的重试排在正在跑的那一轮后面，那一轮失败、把它放弃了：排着的这次投递不能当成处理完
    确认掉（那样它既没跑过，也不会进死信），要算处理失败，让通信机制再投——再投来就重新收下。"""
    current = await wake.set_next_wake(now_cst(), "到点了。")
    gate = asyncio.Event()

    async def plan():
        seen = world.runner.runs[-1][-1].content
        if "你给自己排的一次醒来" in seen and "跑不完的那条。" in seen:
            await gate.wait()
        if "跑不完的那条。" in seen:
            raise RuntimeError("这一轮跑不完")
        return await sets_wake()()

    world.runner.plan = plan
    stuck = _from("赤尾", "跑不完的那条。")
    for _ in range(receiving.PROCESSING_RETRY.n - 1):
        with pytest.raises(RuntimeError):
            await _soon(world.deliver(stuck))

    its_time = asyncio.create_task(world.deliver(self_message(current.message_id, "到点了。")))
    await eventually(lambda: len(world.runner.runs) == receiving.PROCESSING_RETRY.n, timeout=5)
    added: list[str] = []
    real_add = pending.add

    def add(message):
        real_add(message)
        added.append(message.message_id)

    monkeypatch.setattr(pending, "add", add)
    retry = asyncio.create_task(world.deliver(stuck))
    await eventually(lambda: added == [stuck.message_id], timeout=5)
    gate.set()  # 这一轮失败：它的失败数到了，放弃
    outcomes = await _soon(asyncio.gather(its_time, retry, return_exceptions=True))

    assert isinstance(outcomes[0], RuntimeError)
    assert isinstance(outcomes[1], RoundFailed), "排着的那次投递被当成处理完确认掉了"

    world.runner.plan = sets_wake()
    await world.deliver(stuck)  # 通信机制再投的那一次
    assert "跑不完的那条。" in _round_input(world)
    assert pending.read() == []


async def test_its_own_wake_is_never_given_up(world):
    current = await wake.set_next_wake(now_cst(), "到点了。")
    plan = _fails_while_it_sees("跑不完的那条。")
    plan.world = world
    world.runner.plan = plan
    with pytest.raises(RuntimeError):
        await world.deliver(_from("赤尾", "跑不完的那条。"))
    its_time = self_message(current.message_id, "到点了。")

    for _ in range(receiving.PROCESSING_RETRY.n + 2):  # 自定醒来不限次数重试，直到有一次处理成功
        try:
            await world.deliver(its_time)
            break
        except RuntimeError:
            pass
    else:
        pytest.fail("自定醒来一直没跑完")

    # 跑不完的那条在第 n 次失败之后被放弃；自定唤醒一直在，下一轮就跑完了。
    assert "到点了。" in _round_input(world) and "跑不完的那条。" not in _round_input(world)
    assert wake.read_next_wake().message_id != current.message_id


# ---------------------------------------------------------------------------
# 一轮的时限，一次投递最多等多久
# ---------------------------------------------------------------------------


async def test_a_round_over_its_time_fails_every_delivery_it_took(world):
    gate = asyncio.Event()
    taken: list[list[str]] = []

    async def run(messages):
        taken.append([m.body for m in messages])
        if len(taken) == 1:
            await gate.wait()
            return
        await asyncio.sleep(10)

    rounds = Rounds(run, round_timeout=timedelta(seconds=0.2))
    running = asyncio.create_task(rounds.receive(_from("赤尾", "我出门了。")))
    await eventually(lambda: taken, timeout=5)
    later = []
    for message in (_from("千凪", "我在做饭。"), _from("绫奈", "我在看书。")):
        later.append(asyncio.create_task(rounds.receive(message)))
        await _arrived(message)
    gate.set()
    outcomes = await asyncio.gather(running, *later, return_exceptions=True)

    assert outcomes[0] is None
    assert isinstance(outcomes[1], TimeoutError)
    assert isinstance(outcomes[2], RoundFailed)
    assert taken[1] == ["我在做饭。", "我在看书。"]
    assert [m.body for m in pending.read()] == ["我在做饭。", "我在看书。"]


def test_a_delivery_may_wait_for_the_round_ahead_and_then_its_own():
    async def run(messages):
        return None

    rounds = Rounds(run, round_timeout=timedelta(minutes=30))
    assert rounds.delivery_timeout > timedelta(minutes=60)


# ---------------------------------------------------------------------------
# 收件箱的记录读不出来
# ---------------------------------------------------------------------------


async def test_an_unreadable_record_of_the_inbox_is_set_aside_and_rounds_go_on(
    world, volume, caplog
):
    path = volume / LANE / "pending.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{不是 JSON", encoding="utf-8")

    await world.deliver(_from("赤尾", "我出门了。"))

    assert len(world.runner.runs) == 1
    [aside] = list((volume / LANE).glob("pending.json.unreadable-*"))
    assert aside.read_text(encoding="utf-8") == "{不是 JSON"
    assert any(str(aside) in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


# ---------------------------------------------------------------------------
# 处理完的记录留到这条消息有一次投递被答复为止，不按时间删
# ---------------------------------------------------------------------------


def _handled_records(volume) -> dict:
    path = volume / LANE / "pending.json"
    return json.loads(path.read_text(encoding="utf-8"))["handled"] if path.exists() else {}


async def test_a_message_handled_while_its_delivery_was_away_runs_once_even_days_later(
    world, volume, monkeypatch
):
    """A 自己那一轮失败了，它的投递在等重试；B 那一轮把 A 一起处理完。world 停了好几天（coe
    泳道常常一下就是几天），A 的重试这时才回来：不再跑第二遍。"""
    world.runner.plan = in_turn(world, sets_nothing(), *[sets_wake()] * 3)
    away = _from("千凪", "我在做饭。")
    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(away)
    await world.deliver(_from("赤尾", "我出门了。"))
    assert "我在做饭。" in _round_input(world)

    days_later = now_cst() + timedelta(days=3)
    monkeypatch.setattr(pending, "now_cst", lambda: days_later)
    restart(world)
    await world.deliver(_from("绫奈", "我回来了。"))  # 起来之后先有别的一轮，记录照样写过几遍
    await world.deliver(away)  # A 的重试

    assert len(world.runner.runs) == 3
    assert sum("我在做饭。" in run[-1].content for run in world.runner.runs) == 2  # 失败那轮和 B 那轮
    assert _handled_records(volume) == {}, "答复过之后这条记录就不用留了"


async def test_no_record_is_left_for_messages_whose_delivery_was_answered(world, volume):
    """等着这一轮的投递在这一轮跑完时就答复了，不叫醒的那条送到时就答复了：之后再来的同一条
    由通信机制挡住，文件里不留它们的记录，不会越写越长。"""
    await world.deliver(_from("绫奈", "我在看书。", wakes=False))
    await world.deliver(_from("赤尾", "我出门了。"))

    assert len(world.runner.runs) == 1
    assert _handled_records(volume) == {}


async def test_a_message_whose_round_finished_starts_no_round_if_the_process_died_before_answering(
    world, monkeypatch
):
    """这一轮跑完、记成处理完了，进程死在答复它的投递之前：broker 把那次投递重投给下一个进程，
    不再起一轮。"""
    message = _from("赤尾", "我出门了。")
    real = pending.answered

    def dies(message_id):
        monkeypatch.setattr(pending, "answered", real)
        raise Crash()

    monkeypatch.setattr(pending, "answered", dies)
    with pytest.raises(Crash):
        await world.deliver(message)

    restart(world)
    await world.deliver(message)

    assert len(world.runner.runs) == 1
