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
    running = asyncio.create_task(deliver_first)
    await eventually(lambda: len(world.runner.runs) == 1, timeout=5)
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


async def test_a_message_a_finished_round_took_starts_no_round_when_it_comes_again(world):
    message = _from("赤尾", "我出门了。")
    await world.deliver(message)

    await world.deliver(message)  # 进程死在处理完和确认之间，broker 重投
    restart(world)
    await world.deliver(message)  # 换了进程也一样

    assert len(world.runner.runs) == 1


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
    await world.deliver(second)
    assert len(world.runner.runs) == 3


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
    await world.deliver(quiet)
    assert len(world.runner.runs) == 1


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


# ---------------------------------------------------------------------------
# 一直跑不完的消息：经过的失败轮数跟通信机制给一次投递的处理次数一样多，就不再带进之后的轮
# ---------------------------------------------------------------------------


def _fails_while_it_sees(body: str):
    async def plan():
        if body in plan.world.runner.runs[-1][-1].content:
            raise RuntimeError("这一轮跑不完")
        return await sets_wake()()

    return plan


async def test_a_message_in_as_many_failed_rounds_as_a_delivery_gets_tries_is_given_up(
    world, caplog
):
    plan = _fails_while_it_sees("跑不完的那条。")
    plan.world = world
    world.runner.plan = plan
    stuck = _from("赤尾", "跑不完的那条。")

    for _ in range(receiving.PROCESSING_RETRY.n):
        with pytest.raises(RuntimeError):
            await world.deliver(stuck)

    [gave_up] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert stuck.message_id in gave_up.getMessage()
    tries = len(world.runner.runs)
    await world.deliver(stuck)  # 它的投递再来：不再起一轮
    assert len(world.runner.runs) == tries

    await world.deliver(_from("绫奈", "我在看书。"))
    assert len(world.runner.runs) == tries + 1
    assert "跑不完的那条。" not in _round_input(world)


async def test_its_own_wake_is_never_given_up(world):
    current = await wake.set_next_wake(now_cst(), "到点了。")
    plan = _fails_while_it_sees("跑不完的那条。")
    plan.world = world
    world.runner.plan = plan
    with pytest.raises(RuntimeError):
        await world.deliver(_from("赤尾", "跑不完的那条。"))
    its_time = self_message(current.message_id, "到点了。")

    for _ in range(receiving.PROCESSING_RETRY.n + 2):
        try:
            await world.deliver(its_time)
        except RuntimeError:
            pass

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


async def test_a_record_of_a_handled_message_is_kept_a_day_then_dropped(
    world, volume, monkeypatch
):
    message = _from("赤尾", "我出门了。")
    await world.deliver(message)
    assert pending.is_done(message.message_id)

    later = now_cst() + pending.KEPT_FOR + timedelta(minutes=1)
    monkeypatch.setattr(pending, "now_cst", lambda: later)
    await world.deliver(_from("绫奈", "我在看书。"))

    assert not pending.is_done(message.message_id)
    stored = json.loads((volume / LANE / "pending.json").read_text(encoding="utf-8"))
    assert message.message_id not in stored["handled"]
