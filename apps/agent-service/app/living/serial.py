"""并发纪律：进程内排他占用，以及按提交顺序 append。

两件事共用一个原语。

**一、同一个 persona 的缝必须串行。** 会有两条路把同一个 life 带到下一刻（固定
循环 + 强提醒提前的那一缝），它们一定并发。后到的那次要**排队等前一次做完**，
不是被丢弃——丢弃会让她漏掉事情。所以这里是一把会阻塞的互斥锁，不是
``app.runtime.single_flight``（那个撞上直接 raise，语义是"丢掉"）。

**二、共享记录要有稳定的消费顺序。** 三个 life 加 world 并发写同一份记录。若按
"发生时间"开时间窗，一条提交晚于窗口推进的记录会被永久越过；自然键幂等只防重复
行，防不了这个。:func:`append_in_commit_order` 在占用里分配 ``seq``、占用放开前
记录已经落库提交，所以 **seq 的先后 == 提交的先后**，任一时刻可见的 seq 集合都是
一段连续前缀。读侧游标推到"本次读到的最大 seq"就绝不会跳过任何东西。

前提：agent-service 单副本
--------------------------

这把锁只在**一个进程内**互斥。够用的依据是 agent-service 只有一个副本——world 和
三个 life 都跑在同一个进程的同一个事件循环里。

**这个前提不是这里挑出来的，是整个设计本来就压在上面的。** framework 的 interval
time source 在多副本下每个副本各跑一份定时循环：同一缝会被推进两遍，她一次醒两回。
那是发生在锁之外的重复，换成跨进程的锁也拦不住。所以哪天真要上多副本，**要先给
time source 做 leader election，不是先把这把锁换成跨进程的**——先换锁只会把"双跑"
从看得见的重复变成看不见的重复。

为什么不再用 postgres 的 session 级 advisory lock（上一版是那个）：

  * **持锁者和每个等待者各占一条业务连接。** 缝那把锁要持有整轮（含几十秒的模型
    调用），池是 10 + overflow；一旦积压，持锁者自己会因为拿不到连接而失败，把
    别人也一起卡死。
  * **锁连接在模型调用期间断开会静默释放。** session 级 advisory lock 随连接消失
    而消失，可是旧的 body 还在跑——第二个 body 同时进入，双跑，而且谁都不知道。
    asyncio 锁没有这条：持有它的协程死了才轮到下一个，没有"锁没了但活还在跑"。

**不要嵌套同一个 key**：``asyncio.Lock`` 不可重入，同 key 嵌套 = 永久自锁死。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, key_fields
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent, select_latest

T = TypeVar("T", bound=Data)

# key -> 锁，按事件循环分桶。
#
# 线上只有一个事件循环（uvicorn 起的那个），分桶纯粹是因为 ``asyncio.Lock`` 在第一
# 次真正排队时会绑死当时的循环，跨循环复用会 RuntimeError；而 pytest 给每个用例一个
# 新循环。WeakKeyDictionary 让循环被回收时那一桶自己消失，不留全局残留。
_locks: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = WeakKeyDictionary()


def _lock_for(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    bucket = _locks.get(loop)
    if bucket is None:
        bucket = {}
        _locks[loop] = bucket
    lock = bucket.get(key)
    if lock is None:
        lock = asyncio.Lock()
        bucket[key] = lock
    return lock


@asynccontextmanager
async def hold(key: str) -> AsyncIterator[None]:
    """占住 ``key``；已被别人占着就**排队等**，等到为止。

    ``asyncio.Lock`` 的等待是 FIFO 的：先来的先拿到。这条是
    :func:`append_in_commit_order` 那个"下一个拿到占用的人一定看得见上一个人写的
    行"论证的一半——另一半是那边的 commit 落在放开占用之前。
    """
    async with _lock_for(key):
        yield


async def append_in_commit_order(
    cls: type[T], *, stream: str, scope: dict[str, Any], **fields: Any
) -> T:
    """给 ``cls`` 分配 ``stream`` 上的下一个 ``seq`` 并落库，返回落库的那一行。

    ``scope`` 是这条 seq 轴的范围（比如 ``{"lane": ...}``，或者 whereabouts 的
    ``{"lane": ..., "persona_id": ...}``）：既是 ``MAX(seq)`` 的过滤条件，也直接
    进这一行——轴的范围就是行上的那几个字段，让调用方写两遍必然写歪。

    撞上同一自然键（重放）时不插入，返回库里已有的那一行——刚取的号作废，在 seq
    轴上留一个**永远为空的洞**。洞不影响读：读侧问的是"seq 比游标大的行"，一个
    从来没出现过的号不会让任何人被跳过。

    **"锁放开前该行已可见"在进程内锁下仍然成立**，依据是这两句都在 ``hold`` 里：
    取号的 ``MAX(seq)`` 和落库的 ``insert_idempotent``。后者用 ``get_session()``，
    退出即 commit，而这个退出发生在 ``hold`` 退出之前。加上 ``asyncio.Lock`` 的
    FIFO 等待，下一个拿到占用的人一定在那次 commit 之后才开始跑。所以任一时刻
    可见的 seq 集合都是一段连续前缀，不会出现"seq 7 可见、seq 6 还在飞"。
    """
    table = _table_name(cls)
    where = " AND ".join(f"{col} = :{col}" for col in scope)
    async with hold(stream):
        async with get_session() as s:
            result = await s.execute(
                text(
                    f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table} "
                    f"WHERE {where}"
                ),
                scope,
            )
            seq = int(result.scalar_one())
        row = cls(seq=seq, **scope, **fields)
        if await insert_idempotent(row):
            return row

    existing = await select_latest(
        cls, {k: getattr(row, k) for k in key_fields(cls)}
    )
    assert existing is not None, (
        f"{cls.__name__} 插入被 ON CONFLICT 挡下，却查不到已存在的行 —— "
        f"自然键 {key_fields(cls)} 和 dedup 口径对不上"
    )
    return existing  # type: ignore[return-value]
