"""同一个 persona 的缝必须串行——后到的排队等，不是被丢弃。

两条路会把同一个 life 带到下一刻（固定循环 + 强提醒提前的那一缝），它们
一定并发。这里验的是占用本身：并发进入同一个 key 时区间不重叠、两次都
跑完；不同 key 互不阻塞；而且**不占数据库连接**。
"""
from __future__ import annotations

import asyncio

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
            "hold() 拿了数据库连接：缝那把锁要持有整轮（含几十秒模型调用），"
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
