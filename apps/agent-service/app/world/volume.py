"""world 的私有卷：``$WORLD_DATA_DIR/<泳道>/`` 这个目录，以及往里写文件的唯一方式。

``WORLD_DATA_DIR`` 由部署给（world App 的 env），指向私有卷上 world 专用的那个根目录。
下面按进程的部署泳道分目录（prod 写成 ``prod``）：泳道那一段由代码拼，不让每条泳道各配
一个路径——忘了配的后果是静默写进别的泳道。路径本身不是世界内容。

目录里放这几样：``records/``（记录，:mod:`app.world.records`）、``sources/<来源>/``（各知识
来源自己存的东西，:func:`app.world.sources.private_dir`）、私有状态文件（下次醒来，
:mod:`app.world.wake`；没跑完的一轮里已经发生的事，:mod:`app.world.unfinished`；收件箱里的消息
走到了哪一步，:mod:`app.world.pending`）和写锁文件。
人工读写接口只够得到 ``records/``。

**这里的读写全是同步的。** 文件都很小，卷在本机挂载；同步读写意味着"检查指纹"和
"写下去"之间没有 ``await``，同一个进程里的两个写者（主 agent 的工具、人工接口）不可能
在这两步之间插进来，也不存在"协程被取消了而线程还在写"的情况。

**同一时刻只有一个 world 进程写这条泳道的目录**，靠目录里那个锁文件上的 flock 独占锁
（:func:`writer_lock`）。发布是滚动更新，单副本也会有新旧两个 world 进程同时在跑的一段；
卷是 hostPath 卷、固定在单节点，挂它的 pod 都在同一台宿主机上读写同一个本地目录，flock
在它们之间是真的互斥。拿着锁的进程才消费收件箱（收件箱开设时声明了 ``consume_while``，
见 :mod:`app.world.wiring`），启动补醒也在拿到锁之后才跑；没拿到的进程照常起来、照常答
HTTP，读记录照常，写一律拒绝（:class:`WriterLockNotHeld`）。进程退出时先停消费、等正在
处理的那一轮，最后才放锁（:func:`app.messaging.receiving.stop_receiving`）；进程要是直接
死了，内核关掉它的文件，锁也跟着放开。
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from app.infra.cst_time import now_cst
from app.runtime.lane_policy import current_deployment_lane

logger = logging.getLogger(__name__)

DATA_DIR_ENV = "WORLD_DATA_DIR"

WRITER_LOCK_FILE = "writer.lock"
# 锁在别的进程手上时，隔多久再试一次（秒）。
WRITER_LOCK_POLL_SECONDS = 2.0

# 这个进程拿着的写锁：锁文件的路径和打开它的那个文件描述符。
_held: tuple[Path, int] | None = None


class VolumeUnavailable(RuntimeError):
    """``WORLD_DATA_DIR`` 没配置：world 没有地方放它的记录和状态。"""


class WriterLockNotHeld(RuntimeError):
    """这个进程没拿着这条泳道的写锁：另一个 world 进程正在写，这次写入没有做。"""


def lane_dir() -> Path:
    """这条泳道在私有卷上的目录（不一定已经存在）。"""
    root = os.getenv(DATA_DIR_ENV)
    if not root:
        raise VolumeUnavailable(f"{DATA_DIR_ENV} 没有配置：world 没有地方放它的记录和状态")
    return Path(root) / (current_deployment_lane() or "prod")


def _lock_path() -> Path:
    return lane_dir() / WRITER_LOCK_FILE


def try_acquire_writer_lock() -> bool:
    """试一次拿这条泳道的写锁，不等。已经拿着就是 ``True``。"""
    global _held
    path = _lock_path()
    if _held is not None and _held[0] == path:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    _held = (path, fd)
    return True


def release_writer_lock() -> None:
    global _held
    if _held is None:
        return
    _path, fd = _held
    _held = None
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@asynccontextmanager
async def writer_lock() -> AsyncIterator[None]:
    """拿着这条泳道的写锁期间。锁在别的进程手上就每隔 :data:`WRITER_LOCK_POLL_SECONDS` 再试。"""
    waiting = False
    while not try_acquire_writer_lock():
        if not waiting:
            logger.info("world: another world process holds the writer lock; waiting")
            waiting = True
        await asyncio.sleep(WRITER_LOCK_POLL_SECONDS)
    logger.info("world: holding the writer lock")
    try:
        yield
    finally:
        release_writer_lock()
        logger.info("world: released the writer lock")


def require_writer_lock() -> None:
    """这个进程拿着这条泳道的写锁吗；没拿着就抛 :class:`WriterLockNotHeld`。"""
    if _held is None or _held[0] != _lock_path():
        raise WriterLockNotHeld(
            "这个 world 进程没有拿到卷的写锁（另一个 world 进程正在写这条泳道），这次写入没有做"
        )


def write_atomically(target: Path, text: str) -> None:
    """整份写下 ``target``：先写同目录下的临时文件，再原子地换过去。只有拿着写锁的进程能写。

    读的人要么看到旧的一整份，要么看到新的一整份；进程死在中途，留下的是一个以点开头的
    临时文件，列目录时不算数。
    """
    require_writer_lock()
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def set_aside(path: Path) -> Path:
    """把读不出来的一份文件原样改名留在同一个目录（``<名字>.unreadable-<时刻>``），交回新的路径。

    读的一方接着按"没有这份文件"往下走：之后写的是一份新文件，挪开的那份不会被覆盖或者删掉，
    留给人看过之后处理。只有拿着写锁的进程能挪。
    """
    require_writer_lock()
    aside = path.with_name(f"{path.name}.unreadable-{now_cst():%Y%m%dT%H%M%S%f}")
    os.replace(path, aside)
    return aside
