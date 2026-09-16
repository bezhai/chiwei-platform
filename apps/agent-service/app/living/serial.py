"""并发纪律：进程内排他占用，以及按提交顺序 append。

两件事共用一个原语。

**一、同一个 persona 的醒来必须串行。** 会有两条路把同一个 life 带到下一刻（固定
循环 + 强提醒提前的那一次），它们一定并发。后到的那次要**排队等前一次做完**，
不是被丢弃——丢弃会让她漏掉事情。所以这里是一把会阻塞的互斥锁，不是撞上就 raise 的
单飞闸（那种语义是"丢掉"）。

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
time source 在多副本下每个副本各跑一份定时循环：同一轮会被推进两遍，她一次醒两回。
那是发生在锁之外的重复，换成跨进程的锁也拦不住。所以哪天真要上多副本，**要先给
time source 做 leader election，不是先把这把锁换成跨进程的**——先换锁只会把"双跑"
从看得见的重复变成看不见的重复。

为什么不再用 postgres 的 session 级 advisory lock（上一版是那个）：

  * **持锁者和每个等待者各占一条业务连接。** 这把锁要在一次醒来的全程持有（含几十秒的模型
    调用），池是 10 + overflow；一旦积压，持锁者自己会因为拿不到连接而失败，把
    别人也一起卡死。
  * **锁连接在模型调用期间断开会静默释放。** session 级 advisory lock 随连接消失
    而消失，可是旧的 body 还在跑——第二个 body 同时进入，双跑，而且谁都不知道。
    asyncio 锁没有这条：持有它的协程死了才轮到下一个，没有"锁没了但活还在跑"。

**不要嵌套同一个 key**：``asyncio.Lock`` 不可重入，同 key 嵌套 = 永久自锁死。

占用有上限，因为「炸了会放开」挡不住「不结束」
----------------------------------------------

2026-09-16 prod 实证：一次挂住的调用（不返回、也不超时）让 akao 和 chinagi 停摆 15
小时，同一天 coe-living 的 world 停摆 8 小时。三处的形状一样 —— 进程活着、别的 key
照跑、**日志、trace、报错一个都没有**：时间源投拍是 fire-and-forget，后面每一拍都
静悄悄排在这把锁后面等，而等待没有尽头。

所以 :data:`HELD_SECONDS` 给占用封了个顶。它**不是给她的轮次设预算** —— 值取得远
大于任何一轮正常该花的时间（校准见常量处），唯一的作用是把"永远"变成"有限"。到顶
了掐断那一轮，走的是**既有的崩溃恢复语义**：占用随之放开，下一拍重来，而轮次本来
就按"中途崩掉会重跑"设计（派生 id + CAS + 幂等写）。

这**不等于**"掐断不留下任何中间状态"。已经发出去、结果未知的那一类（比如 mouth 那
边在发消息前后被取消）照旧要靠原有的对账收尾 —— 这个顶没有让它们变干净，只是把
"卡死"换成了"崩溃"这种已经有人管的形状。

取消是协作式的，所以有两种 body 掐不断：纯 CPU 打转、从不 await 的，和把
``CancelledError`` 吞掉照常走完的。前者整个事件循环本来就已经停了，不是这里能兜的；
后者会正常返回、不抛 ``TimeoutError``，但占用仍然在它返回时放开。

**这一层不替代下面该有的超时。** 真正该掐的是模型调用、HTTP、数据库各自那一层；
这里兜的是"不管哪一层漏了，占用都不会被永久扣住"。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, key_fields
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent, select_latest

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Data)

# 一次占用最长持有多久。见模块 docstring：这是死锁兜底，不是轮次预算。
#
# 按 prod 实测校准，不是拍的：2026-09-16 取 Langfuse 上 1413 条 prod 轮次，
# p50 8.5 秒、p95 76.4 秒、p99 121.8 秒、**最长 188.9 秒**（world 的一轮）。900 是
# 实测最长值的 4.8 倍。注意这个分布只统计得到跑完的轮次 —— 卡死的那些根本没有
# trace，所以它回答的是"正常一轮能有多慢"，不是"卡死前能拖多久"，而前者正是这个
# 顶该躲开的东西。
#
# 哪天真在日志里看到 `占住超过` 而那一轮其实是正常的慢，说明这个分布变了，该重新
# 量一次再调，不要顺手往上加。
HELD_SECONDS = 900.0

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
async def hold(key: str, *, seconds: float | None = None) -> AsyncIterator[None]:
    """占住 ``key``；已被别人占着就**排队等**，等到为止。

    ``asyncio.Lock`` 的等待是 FIFO 的：先来的先拿到。这条是
    :func:`append_in_commit_order` 那个"下一个拿到占用的人一定看得见上一个人写的
    行"论证的一半——另一半是那边的 commit 落在放开占用之前。

    拿到之后最多持有 ``seconds`` 秒（不传就用 :data:`HELD_SECONDS`），到点把这一轮
    掐断并抛 ``TimeoutError``，占用随之放开。为什么要有这个顶见模块 docstring。
    """
    cap = HELD_SECONDS if seconds is None else seconds
    async with _lock_for(key):
        try:
            async with asyncio.timeout(cap) as cutoff:
                yield
        except TimeoutError:
            # 只认**这一层**到点。body 自己抛 TimeoutError（HTTP 超时之类），或者
            # 内层嵌套的另一把占用到点，都会以 TimeoutError 穿过这里 —— 照单全收
            # 就会报出一条"某某占住超过 900 秒"的假账，而这个顶存在的全部意义正是
            # 出事时能一眼看出是谁卡住了。
            if cutoff.expired():
                logger.warning(
                    "living serial: %s 占住超过 %.0f 秒，这一轮被掐断 —— "
                    "当它崩在那儿处理，占用即将放开，下一拍重来",
                    key,
                    cap,
                )
            raise


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
