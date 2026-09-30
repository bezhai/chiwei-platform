"""一轮收尾定下次醒来时，每一步都可能失败或者进程死在中间：逐个顺序看 world 会不会停转。

定下次醒来是**先发后记**（:func:`app.world.wake.set_next_wake`）：先用 send_at 排出新时刻 B，
broker 确认之后才把 B 记成私有状态里的"最新唤醒"。所以状态里记的唤醒，一定已经排出去了，
或者就是正在处理、正在重试的那一条。自定消息的 id 跟"最新唤醒"对不上，就是旧消息，跳过。
状态里的最新唤醒处理失败时不限次数重试，永不进死信
（:func:`app.world.wake.retry_latest_wake_without_limit`）。

A 是叫醒这一轮的自定消息（状态里的最新唤醒），B 是这一轮定的新时刻。"通信机制重投 A"在这里
就是再调一次 :func:`app.world.main_agent.on_world_message`；"进程死了再起来"就是收件箱开设时
的 :func:`app.world.wake.wake_on_start`，加上没确认的消息被重投。

每个用例最后都用 :func:`_assert_awake` 核对同一条不变量：之后一定还有一条会被执行的自定唤醒
——它已经排出去了，不会被当成旧消息跳过，失败了也不会进死信。

| 行 | 出事的位置                               | 接下来                                               |
|----|------------------------------------------|------------------------------------------------------|
| 1  | 模型那一段失败、没定时刻                 | 状态不变；A 不限次重试，再跑                         |
| 2  | send B 失败，broker 没收到               | 状态不变；A 重试再跑，定下 C                         |
| 3  | send B 报失败，其实 broker 收到了        | 状态不变；B 到点是旧消息；A 重试定下 C               |
| 4  | send B 成功，写状态失败                  | 同 3                                                 |
| 5  | 进程死在 send B 与写状态之间             | 重启：A 的时刻已过，立刻排 S；A 重投、B 都是旧消息  |
| 6  | 进程死在写状态与确认 A 之间              | A 重投是旧消息；B 已排出、是最新                     |
| 7  | 一轮超时，取消落在 send 等确认时         | 同 2                                                 |
| 8  | 别人的消息触发的一轮在收尾失败           | 原定的 W 仍是最新、在队列里；那条消息照常有限次重试  |
| 9  | 状态里的最新唤醒连续失败 N 次            | 每一次都不限次重试，永不进死信（真 broker 那条在     |
|    |                                          | ``test_wakes_on_a_real_broker.py``）                 |
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.infra.cst_time import now_cst
from app.messaging.message import Kind, SendFailed, new_message
from app.world import main_agent, wake

from .conftest import self_message, sets_nothing, sets_wake


class Crash(BaseException):
    """进程在这一步死了：什么都接不住它。"""


def _fail_next_send(monkeypatch, world, *, queued: bool, error: BaseException | None = None):
    """下一次 send_at 失败；``queued`` 表示 broker 其实已经收下了（记进 ``world.scheduled``）。"""
    calls = {"n": 0}

    async def send_at(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            if queued:
                world.scheduled.append(kw)
            raise error or SendFailed("broker did not confirm", message_id=kw["message_id"])
        world.scheduled.append(kw)
        return kw["message_id"]

    monkeypatch.setattr(wake, "send_at", send_at)


def _fail_next_record(monkeypatch, error: BaseException):
    """下一次把最新唤醒写进私有状态时失败（之后的照常写）。"""
    real = wake._record_next_wake
    calls = {"n": 0}

    def record(next_wake):
        calls["n"] += 1
        if calls["n"] == 1:
            raise error
        real(next_wake)

    monkeypatch.setattr(wake, "_record_next_wake", record)


async def _armed(world):
    """A：状态里的最新唤醒，已经排出去了，到点了。"""
    a = await wake.set_next_wake(now_cst(), "到点了。")
    world.scheduled.clear()
    world.scheduled.append({"message_id": a.message_id})  # 它早就排出去了
    return a, self_message(a.message_id, "到点了。")


async def _never_dead_lettered(message) -> bool:
    return await wake.retry_latest_wake_without_limit(message) is not None


async def _assert_awake(world):
    """之后一定还有一条会被执行的自定唤醒：已排出、不是旧消息、失败了也不进死信。"""
    latest = wake.read_next_wake()
    assert latest is not None
    assert latest.message_id in {s["message_id"] for s in world.scheduled}, "最新唤醒没排出去"
    message = self_message(latest.message_id)
    assert not wake.is_stale_wake(message), "最新唤醒会被当成旧消息跳过"
    assert await _never_dead_lettered(message), "最新唤醒失败时会进死信"


async def _assert_retried_forever(trigger):
    """这一轮失败了：叫醒它的那条重投时照常跑，而且无论失败几次都不进死信。"""
    assert not wake.is_stale_wake(trigger)
    assert await _never_dead_lettered(trigger)


async def test_row_1_the_model_part_fails(world):
    a, trigger = await _armed(world)
    world.runner.plan = sets_nothing()

    with pytest.raises(main_agent.NoNextWake):
        await main_agent.on_world_message(trigger)
    assert wake.read_next_wake() == a
    await _assert_retried_forever(trigger)

    world.runner.plan = sets_wake()
    await main_agent.on_world_message(trigger)
    assert len(world.runner.runs) == 2
    await _assert_awake(world)


async def test_row_2_sending_the_new_wake_fails(world, monkeypatch):
    a, trigger = await _armed(world)
    _fail_next_send(monkeypatch, world, queued=False)

    with pytest.raises(SendFailed):
        await main_agent.on_world_message(trigger)
    assert wake.read_next_wake() == a, "没排出去的唤醒不能记进状态"
    await _assert_retried_forever(trigger)

    await main_agent.on_world_message(trigger)
    assert len(world.runner.runs) == 2
    await _assert_awake(world)


async def test_row_3_sending_reports_failure_but_the_broker_has_it(world, monkeypatch):
    a, trigger = await _armed(world)
    _fail_next_send(monkeypatch, world, queued=True)

    with pytest.raises(SendFailed):
        await main_agent.on_world_message(trigger)
    b = world.scheduled[-1]["message_id"]
    await main_agent.on_world_message(self_message(b))  # B 先到
    assert len(world.runner.runs) == 1, "没记进状态的 B 应当是旧消息"
    await _assert_retried_forever(trigger)

    await main_agent.on_world_message(trigger)
    assert len(world.runner.runs) == 2
    assert wake.is_stale_wake(self_message(b))
    await _assert_awake(world)


async def test_row_4_recording_fails_after_the_new_wake_was_sent(world, monkeypatch):
    a, trigger = await _armed(world)
    _fail_next_record(monkeypatch, OSError("disk full"))

    with pytest.raises(OSError):
        await main_agent.on_world_message(trigger)
    b = world.scheduled[-1]["message_id"]
    assert wake.read_next_wake() == a
    await _assert_retried_forever(trigger)

    await main_agent.on_world_message(trigger)
    assert len(world.runner.runs) == 2
    assert wake.is_stale_wake(self_message(b))
    await _assert_awake(world)


async def test_row_5_dying_between_sending_and_recording(world, monkeypatch):
    a, trigger = await _armed(world)
    _fail_next_record(monkeypatch, Crash())

    with pytest.raises(Crash):
        await main_agent.on_world_message(trigger)
    b = world.scheduled[-1]["message_id"]
    await wake.wake_on_start()  # 新进程：A 的时刻已经过了

    started = wake.read_next_wake()
    assert started.message_id not in {a.message_id, b}
    await main_agent.on_world_message(trigger)  # 没确认的 A 被重投
    await main_agent.on_world_message(self_message(b))
    assert len(world.runner.runs) == 1, "A 和 B 都该是旧消息"
    await _assert_awake(world)


async def test_row_6_dying_between_recording_and_acknowledging(world):
    a, trigger = await _armed(world)
    await main_agent.on_world_message(trigger)
    b = world.scheduled[-1]["message_id"]

    await main_agent.on_world_message(trigger)  # 没确认的 A 被重投

    assert len(world.runner.runs) == 1
    assert wake.read_next_wake().message_id == b
    await _assert_awake(world)


async def test_row_7_a_timeout_lands_while_sending(world, monkeypatch):
    a, trigger = await _armed(world)
    calls = {"n": 0}

    async def slow_then_fine(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(30)
        world.scheduled.append(kw)
        return kw["message_id"]

    monkeypatch.setattr(wake, "send_at", slow_then_fine)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            await main_agent.on_world_message(trigger)
    assert wake.read_next_wake() == a
    await _assert_retried_forever(trigger)

    await main_agent.on_world_message(trigger)
    assert len(world.runner.runs) == 2
    await _assert_awake(world)


@pytest.mark.parametrize("where", ["model", "send", "record"])
async def test_row_8_a_round_woken_by_someone_else_fails(world, monkeypatch, where):
    planned = await wake.set_next_wake(now_cst() + timedelta(hours=3), "原定的。")
    world.scheduled.clear()
    world.scheduled.append({"message_id": planned.message_id})
    message = new_message(sender="operator", recipient="world", body="下雨了。", kind=Kind.MESSAGE)
    if where == "model":
        world.runner.plan = sets_nothing()
    elif where == "send":
        _fail_next_send(monkeypatch, world, queued=False)
    else:
        _fail_next_record(monkeypatch, OSError("disk full"))

    with pytest.raises((main_agent.NoNextWake, SendFailed, OSError)):
        await main_agent.on_world_message(message)

    assert wake.read_next_wake() == planned
    await _assert_awake(world)
    assert await wake.retry_latest_wake_without_limit(message) is None, (
        "别人的消息照常有限次重试"
    )


async def test_row_9_the_latest_wake_failing_again_and_again_is_always_retried(world):
    a, trigger = await _armed(world)
    world.runner.plan = sets_nothing()

    for _ in range(10):
        with pytest.raises(main_agent.NoNextWake):
            await main_agent.on_world_message(trigger)
        await _assert_retried_forever(trigger)

    world.runner.plan = sets_wake()
    await main_agent.on_world_message(trigger)
    assert len(world.runner.runs) == 11
    await _assert_awake(world)
