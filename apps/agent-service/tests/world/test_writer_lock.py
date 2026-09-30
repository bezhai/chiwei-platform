"""同一时刻只有一个 world 进程写卷：这条泳道的写锁在谁手上，谁才写，谁才消费收件箱。

卷是 hostPath 卷、固定在单节点上，挂它的 pod 都在同一台宿主机上读写同一个本地目录，
所以用 flock 做独占锁。flock 按"打开的那个文件"算：同一个进程里另开一次锁文件再去锁，
跟另一个进程去锁是一回事，这里用它模拟"另一个 world 进程拿着锁"。两个真进程的那条在
``test_single_writer.py``。
"""
from __future__ import annotations

import asyncio
import fcntl
import os
from contextlib import contextmanager

import pytest

from app.infra.cst_time import now_cst
from app.world import records, volume, wake

from .conftest import LANE


@contextmanager
def held_by_another_process(root, lane: str = LANE):
    path = root / lane / volume.WRITER_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_while_another_process_holds_the_lock_nothing_is_written(bare_volume):
    records_root = bare_volume / LANE / "records"
    records_root.mkdir(parents=True)
    (records_root / "甲.md").write_text("已有的。", encoding="utf-8")

    with held_by_another_process(bare_volume):
        assert not volume.try_acquire_writer_lock()
        with pytest.raises(volume.WriterLockNotHeld):
            records.write("乙.md", "不该写下。", expected=None)
        fingerprint = records.read("甲.md").fingerprint  # 读照常
        with pytest.raises(volume.WriterLockNotHeld):
            records.delete("甲.md", expected=fingerprint)

    assert sorted(p.name for p in records_root.iterdir()) == ["甲.md"]


async def test_the_wake_state_is_not_written_without_the_lock(bare_volume, monkeypatch):
    async def send_at(**kw):  # pragma: no cover - never reached
        raise AssertionError("没拿到锁就排了消息")

    monkeypatch.setattr(wake, "send_at", send_at)

    with pytest.raises(volume.WriterLockNotHeld):
        await wake.set_next_wake(now_cst(), "x")
    assert wake.read_state() == wake.WakeState()


def test_once_the_other_process_lets_go_this_one_takes_the_lock(bare_volume):
    with held_by_another_process(bare_volume):
        assert not volume.try_acquire_writer_lock()

    assert volume.try_acquire_writer_lock()
    try:
        records.write("甲.md", "拿到锁之后写的。", expected=None)
        with pytest.raises(BlockingIOError), held_by_another_process(bare_volume):
            pass  # 这回轮到别的进程拿不到
    finally:
        volume.release_writer_lock()


async def test_holding_the_lock_waits_until_the_other_process_lets_go(bare_volume, monkeypatch):
    monkeypatch.setattr(volume, "WRITER_LOCK_POLL_SECONDS", 0.05)
    inside = asyncio.Event()

    async def hold() -> None:
        async with volume.writer_lock():
            inside.set()
            await asyncio.sleep(3600)

    with held_by_another_process(bare_volume):
        task = asyncio.create_task(hold())
        await asyncio.sleep(0.3)
        assert not inside.is_set(), "锁在别人手上时进去了"

    await asyncio.wait_for(inside.wait(), timeout=2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # 出来之后锁放开了：别的进程拿得到。
    with held_by_another_process(bare_volume):
        pass


def test_each_lane_has_its_own_lock(bare_volume, monkeypatch):
    with held_by_another_process(bare_volume, lane="coe-other"):
        assert volume.try_acquire_writer_lock()
    volume.release_writer_lock()
