"""同一个 persona 的醒来必须串行——后到的排队等，不是被丢弃。

两条路会把同一个 life 带到下一刻（固定循环 + 强提醒提前的那一次），它们
一定并发。这里验的是占用本身：并发进入同一个 key 时区间不重叠、两次都
跑完；不同 key 互不阻塞；而且**不占数据库连接**。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.living.serial import hold


async def _run(key: str, label: str, seen: list, work: float) -> None:
    async with hold(key):
        seen.append(("enter", label, asyncio.get_running_loop().time()))
        await asyncio.sleep(work)
        seen.append(("leave", label, asyncio.get_running_loop().time()))


class _ExplodingEngine:
    """任何人想拿连接就炸——用来钉死「占用不碰数据库」。"""

    def connect(self):
        raise AssertionError(
            "hold() 拿了数据库连接：这把锁要在一次醒来的全程持有（含几十秒模型调用），"
            "占住业务连接会让连接池在积压时把持锁者自己饿死"
        )

    def begin(self):
        return self.connect()


async def test_hold_does_not_take_a_database_connection(monkeypatch):
    """占用是进程内的，不占业务连接、也不会因为连接断开而静默释放。"""
    from app.data import session as session_mod

    monkeypatch.setattr(session_mod, "engine", _ExplodingEngine())

    async with hold("living:turn:coe-x:akao"):
        pass


async def test_hold_still_serializes_without_a_database(monkeypatch):
    """连数据库都没有的时候，串行语义照样成立——它跟 pg 无关。"""
    from app.data import session as session_mod

    monkeypatch.setattr(session_mod, "engine", _ExplodingEngine())

    seen: list = []
    await asyncio.gather(
        _run("living:turn:coe-x:akao", "loop", seen, 0.05),
        _run("living:turn:coe-x:akao", "nudge", seen, 0.01),
    )
    assert [kind for kind, _, _ in seen] == [
        "enter",
        "leave",
        "enter",
        "leave",
    ], seen


async def test_second_arrival_queues_instead_of_being_dropped():
    """同一 persona 的两次并发进入：后到的等前一次做完，两次都完整跑过。"""
    seen: list = []
    await asyncio.gather(
        _run("living:turn:coe-x:akao", "loop", seen, 0.30),
        _run("living:turn:coe-x:akao", "nudge", seen, 0.05),
    )

    # 两次都跑完（没有一次被丢弃 / 被拒）
    assert sorted(label for kind, label, _ in seen if kind == "enter") == [
        "loop",
        "nudge",
    ]
    assert sorted(label for kind, label, _ in seen if kind == "leave") == [
        "loop",
        "nudge",
    ]
    # 区间严格不重叠：enter/leave 成对出现，不会是 enter,enter,leave,leave
    order = [kind for kind, _, _ in seen]
    assert order == ["enter", "leave", "enter", "leave"], seen


async def test_different_personas_do_not_block_each_other():
    """串行的是「同一个人不能同时想两件事」，不是全局排队。"""
    seen: list = []
    await asyncio.gather(
        _run("living:turn:coe-x:akao", "akao", seen, 0.30),
        _run("living:turn:coe-x:ayana", "ayana", seen, 0.05),
    )

    order = [(kind, label) for kind, label, _ in seen]
    # 两个人区间重叠：谁先抢到连接是随机的，但两个都进去了才有人出来。
    assert [kind for kind, _ in order] == ["enter", "enter", "leave", "leave"], seen
    # 活短的先出来（说明它没在门外等活长的那个）
    assert [label for kind, label in order if kind == "leave"] == [
        "ayana",
        "akao",
    ], seen


async def test_lock_is_released_when_the_body_raises():
    """占用期间炸了也要放开，否则这个 persona 永远醒不过来。"""

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with hold("living:turn:coe-x:akao"):
            raise Boom

    # 还能再进来
    async with hold("living:turn:coe-x:akao"):
        pass


async def test_lock_is_released_when_the_body_never_returns():
    """**一轮永远不返回也必须放开占用。**

    2026-09-16 prod 实证：一次挂住的 HTTP 调用（不回、也不超时）让 akao 和
    chinagi 停摆 15 小时，coe-living 的 world 停摆 8 小时。三处都是进程还活着、
    别的 key 照跑、**日志、trace、报错一个都没有** —— 因为时间源是 fire-and-forget，
    后面每一拍都静悄悄排在这把锁后面。

    「炸了要放开」挡不住这一条：挂死的那一轮没有炸，它只是不结束。
    """
    key = "living:turn:coe-x:akao"

    with pytest.raises(TimeoutError):
        async with hold(key, seconds=0.05):
            await asyncio.Event().wait()

    async with asyncio.timeout(1.0):
        async with hold(key, seconds=1.0):
            pass


async def test_the_next_round_gets_through_after_a_hung_one_times_out():
    """排在挂死那一轮后面的人要能拿到占用 —— 光炸掉持锁者还不够，队列得往下走。"""
    key = "living:turn:coe-x:akao"
    seen: list[str] = []

    async def hangs() -> None:
        with pytest.raises(TimeoutError):
            async with hold(key, seconds=0.05):
                seen.append("hung-enter")
                await asyncio.Event().wait()
        seen.append("hung-timeout")

    async def queued() -> None:
        await asyncio.sleep(0.01)  # 确保排在挂死那一轮后面
        async with hold(key, seconds=1.0):
            seen.append("next-enter")

    async with asyncio.timeout(2.0):
        await asyncio.gather(hangs(), queued())

    assert seen == ["hung-enter", "hung-timeout", "next-enter"], seen


async def test_a_round_inside_the_cap_is_not_cut_short():
    """没超时的活一个字都不能动 —— 超时是死锁兜底，不是给她的轮次设预算。"""
    done: list[str] = []

    async with hold("living:turn:coe-x:akao", seconds=1.0):
        await asyncio.sleep(0.05)
        done.append("finished")

    assert done == ["finished"]


async def test_a_timeout_from_inside_is_not_blamed_on_the_lock(caplog):
    """body 自己抛 ``TimeoutError``（HTTP 超时之类）不算这把锁到点。

    照单全收的话，出事时日志里会多出一条"某某占住超过 900 秒"的假账 —— 而这个顶
    存在的全部意义就是出事时能一眼看出是谁卡住了。
    """
    with caplog.at_level(logging.WARNING, logger="app.living.serial"):
        with pytest.raises(TimeoutError):
            async with hold("living:turn:coe-x:akao", seconds=60.0):
                raise TimeoutError("模型那边超时了，不是这把锁")

    assert "占住超过" not in caplog.text, caplog.text


async def test_an_inner_lock_timing_out_is_not_blamed_on_the_outer(caplog):
    """嵌套时内层到点，记的必须是内层那把 —— 外层刚进来，不该背这个账。

    ``append_in_commit_order`` 就在别的 key 的占用里再占一把。
    """
    with caplog.at_level(logging.WARNING, logger="app.living.serial"):
        with pytest.raises(TimeoutError):
            async with hold("living:turn:coe-x:akao", seconds=60.0):
                async with hold("living:happening:coe-x", seconds=0.05):
                    await asyncio.Event().wait()

    assert "living:happening:coe-x 占住超过" in caplog.text, caplog.text
    assert "living:turn:coe-x:akao 占住超过" not in caplog.text, caplog.text


async def test_being_cancelled_from_outside_is_not_reported_as_a_timeout(caplog):
    """进程关停之类的外部取消要原样往上走，不能被这层改写成超时。"""
    started = asyncio.Event()

    async def body() -> None:
        async with hold("living:turn:coe-x:akao", seconds=60.0):
            started.set()
            await asyncio.Event().wait()

    with caplog.at_level(logging.WARNING, logger="app.living.serial"):
        task = asyncio.create_task(body())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "占住超过" not in caplog.text, caplog.text

    # 取消也把占用放开了
    async with asyncio.timeout(1.0):
        async with hold("living:turn:coe-x:akao", seconds=1.0):
            pass


async def test_there_is_a_cap_even_when_nobody_passes_one(monkeypatch):
    """调用方不传也有上限 —— 今天出事的那几个调用点一个都没传。

    改小默认值再验，而不是在外面套一层 :func:`asyncio.timeout` 等真的默认值：
    套外层的话，超时由外层那一层抛出来，这个用例在「``hold`` 根本没有默认上限」
    时照样会绿。
    """
    from app.living import serial

    assert serial.HELD_SECONDS > 0
    monkeypatch.setattr(serial, "HELD_SECONDS", 0.05)

    with pytest.raises(TimeoutError):
        async with hold("living:turn:coe-x:akao"):
            await asyncio.Event().wait()
