"""一轮收尾定下次醒来时，每一步都可能失败或者进程死在中间：逐个顺序看 world 会不会停转。

收尾三步（:func:`app.world.wake.set_next_wake`）：① 把新时刻 B 作为"待定"写进私有状态；
② 用 send_at 排 B；③ 把 B 升为"当前"。自定消息只要是状态里的"当前"或"待定"，就不算旧消息。

A 是叫醒这一轮的那条自定消息（状态里的"当前"），B 是这一轮定的新时刻。"通信机制重投 A"
在这里就是再调一次 :func:`app.world.main_agent.on_world_message`；"进程死了再起来"就是
收件箱开设时的 :func:`app.world.wake.wake_on_start`，加上没确认的消息被重投。

每个用例最后都用 :func:`_assert_awake` 核对同一条不变量：状态里的"当前"是一条真的排出去了的
消息，或者还有一条会被重跑的消息在路上。

| 行 | 出事的位置                              | 接下来                                      |
|----|-----------------------------------------|---------------------------------------------|
| 1  | 模型那一段失败（没定时刻、抛错）        | A 重投 → 照常跑一轮                         |
| 2  | ① 写"待定"失败                          | 状态没变 → A 重投照常跑                     |
| 3  | ② 排 B 失败，broker 没收到              | A 重投照常跑（修之前被当成旧消息跳过）      |
| 4  | ② 报失败但 broker 收到了，B 先到        | B 是"待定" → 跑一轮；A 再来时是旧消息       |
| 5  | ③ 升"当前"失败，A 先重投                | A 仍是"当前" → 跑一轮                       |
| 6  | ③ 升"当前"失败，B 先到                  | B 是"待定" → 跑一轮（不能被当成旧消息）     |
| 7  | 进程死在 ② 之后、③ 之前                 | 启动时按原 id 补排 B 并升"当前"；A 是旧消息 |
| 8  | 进程死在 ① 之后、② 之前                 | 启动时补排 B 并升"当前"；A 是旧消息         |
| 9  | 进程死在 ③ 之后、确认 A 之前            | A 重投是旧消息；B 已在队列                  |
| 10 | 一轮超时，取消落在 ② 等确认时           | A 重投照常跑                                |
| 11 | 被别人的消息叫醒的一轮在收尾失败        | 那条消息重投照跑；原定的下次醒来还是"当前" |
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.infra.cst_time import now_cst
from app.messaging.message import Kind, SendFailed, new_message
from app.world import main_agent, wake

from .conftest import self_message, sets_nothing


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


def _fail_state_write(monkeypatch, *, on_call: int, error: BaseException):
    """第 ``on_call`` 次写私有状态时失败（之前和之后的照常写）。"""
    real = wake._write_state
    calls = {"n": 0}

    def write(state):
        calls["n"] += 1
        if calls["n"] == on_call:
            raise error
        real(state)

    monkeypatch.setattr(wake, "_write_state", write)


async def _armed(world):
    """A：状态里的"当前"、已经排出去的那条自定消息，到点了。"""
    a = await wake.set_next_wake(now_cst(), "到点了。")
    world.scheduled.clear()
    return a, self_message(a.message_id, "到点了。")


def _assert_awake(world):
    """状态里的"当前"是一条排出去了的消息——world 不会一直睡下去。"""
    current = wake.read_next_wake()
    assert current is not None
    assert current.message_id in {s["message_id"] for s in world.scheduled}


def _new_wakes(world):
    return [s["message_id"] for s in world.scheduled]


async def test_row_1_a_round_that_fails_before_its_wake_runs_again(world):
    a, trigger = await _armed(world)
    world.runner.plan = sets_nothing()

    with pytest.raises(main_agent.NoNextWake):
        await main_agent.on_world_message(trigger)
    assert wake.read_next_wake() == a

    from .conftest import sets_wake

    world.runner.plan = sets_wake()
    await main_agent.on_world_message(trigger)

    assert len(world.runner.runs) == 2
    _assert_awake(world)


async def test_row_2_recording_the_pending_wake_fails(world, monkeypatch):
    a, trigger = await _armed(world)
    _fail_state_write(monkeypatch, on_call=1, error=OSError("disk full"))

    with pytest.raises(OSError):
        await main_agent.on_world_message(trigger)
    assert wake.read_state().current == a and wake.read_state().pending is None
    assert world.scheduled == []

    await main_agent.on_world_message(trigger)

    assert len(world.runner.runs) == 2
    _assert_awake(world)


async def test_row_3_scheduling_fails_before_the_broker_has_it(world, monkeypatch):
    """codex 复现的那条：修之前 A 重投时对不上状态里的 B，被跳过，而 B 从没排出去。"""
    a, trigger = await _armed(world)
    _fail_next_send(monkeypatch, world, queued=False)

    with pytest.raises(SendFailed):
        await main_agent.on_world_message(trigger)
    await main_agent.on_world_message(trigger)

    assert len(world.runner.runs) == 2, "A 重投时被当成了旧消息"
    _assert_awake(world)


async def test_row_4_scheduling_unconfirmed_but_queued_and_the_new_wake_comes_first(
    world, monkeypatch
):
    a, trigger = await _armed(world)
    _fail_next_send(monkeypatch, world, queued=True)

    with pytest.raises(SendFailed):
        await main_agent.on_world_message(trigger)
    [b] = _new_wakes(world)
    await main_agent.on_world_message(self_message(b))
    assert len(world.runner.runs) == 2, "B 是待定，该跑一轮"
    await main_agent.on_world_message(trigger)

    assert len(world.runner.runs) == 2, "B 那一轮定了新时刻之后，A 是旧消息"
    _assert_awake(world)


async def test_row_5_promoting_fails_and_the_trigger_comes_back_first(world, monkeypatch):
    a, trigger = await _armed(world)
    _fail_state_write(monkeypatch, on_call=2, error=OSError("disk full"))

    with pytest.raises(OSError):
        await main_agent.on_world_message(trigger)
    await main_agent.on_world_message(trigger)

    assert len(world.runner.runs) == 2
    _assert_awake(world)


async def test_row_6_promoting_fails_and_the_new_wake_comes_first(world, monkeypatch):
    """B 已经排出去、状态还没把它升为"当前"：B 到点时不能被当成旧消息。"""
    a, trigger = await _armed(world)
    _fail_state_write(monkeypatch, on_call=2, error=OSError("disk full"))

    with pytest.raises(OSError):
        await main_agent.on_world_message(trigger)
    [b] = _new_wakes(world)
    await main_agent.on_world_message(self_message(b))

    assert len(world.runner.runs) == 2, "B 被当成了旧消息"
    _assert_awake(world)


async def test_row_7_dying_between_scheduling_and_promoting(world, monkeypatch):
    a, trigger = await _armed(world)
    real_write = wake._write_state
    _fail_state_write(monkeypatch, on_call=2, error=Crash())

    with pytest.raises(Crash):
        await main_agent.on_world_message(trigger)
    [b] = _new_wakes(world)
    monkeypatch.setattr(wake, "_write_state", real_write)  # 新进程：状态照常写
    world_send = world.scheduled
    await _restart(world, monkeypatch)

    assert wake.read_next_wake().message_id == b
    assert [s["message_id"] for s in world_send].count(b) == 2  # 启动时按原 id 又排了一次
    await main_agent.on_world_message(trigger)  # A 没确认，被重投
    assert len(world.runner.runs) == 1, "B 已经接手，A 是旧消息"
    await main_agent.on_world_message(self_message(b))
    assert len(world.runner.runs) == 2
    _assert_awake(world)


async def test_row_8_dying_between_recording_pending_and_scheduling(world, monkeypatch):
    a, trigger = await _armed(world)
    _fail_next_send(monkeypatch, world, queued=False, error=Crash())

    with pytest.raises(Crash):
        await main_agent.on_world_message(trigger)
    pending = wake.read_state().pending
    assert pending is not None and world.scheduled == []
    await _restart(world, monkeypatch)

    assert wake.read_next_wake() == pending
    assert _new_wakes(world) == [pending.message_id]
    await main_agent.on_world_message(trigger)
    assert len(world.runner.runs) == 1
    _assert_awake(world)


async def test_row_9_dying_after_promoting_before_the_trigger_is_acknowledged(world):
    a, trigger = await _armed(world)
    await main_agent.on_world_message(trigger)
    [b] = _new_wakes(world)

    await main_agent.on_world_message(trigger)  # 没确认的 A 被重投

    assert len(world.runner.runs) == 1
    assert wake.read_next_wake().message_id == b
    _assert_awake(world)


async def test_row_10_a_timeout_lands_while_scheduling(world, monkeypatch):
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
    await main_agent.on_world_message(trigger)

    assert len(world.runner.runs) == 2, "超时之后 A 重投被当成了旧消息"
    _assert_awake(world)


async def test_row_11_a_round_woken_by_someone_else_fails_while_scheduling(world, monkeypatch):
    planned = await wake.set_next_wake(now_cst() + timedelta(hours=3), "原定的。")
    world.scheduled.clear()
    world.scheduled.append({"message_id": planned.message_id})  # 它早就排出去了
    message = new_message(sender="operator", recipient="world", body="下雨了。", kind=Kind.MESSAGE)
    _fail_next_send(monkeypatch, world, queued=False)

    with pytest.raises(SendFailed):
        await main_agent.on_world_message(message)
    assert wake.read_next_wake() == planned
    _assert_awake(world)
    assert not wake.is_stale_wake(self_message(planned.message_id))

    await main_agent.on_world_message(message)
    assert len(world.runner.runs) == 2
    _assert_awake(world)


async def _restart(world, monkeypatch):
    """进程重新起来：收件箱开设时的启动检查。替身 send_at 照旧记账。"""

    async def send_at(**kw):
        world.scheduled.append(kw)
        return kw["message_id"]

    monkeypatch.setattr(wake, "send_at", send_at)
    await wake.wake_on_start()
