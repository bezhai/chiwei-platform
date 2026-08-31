"""谁在哪、对谁、通过什么渠道、做了什么说了什么 —— 写入与两条读取路径。

**两条路径语义不同，不能合成一条。**

  * **定向送达**（她在 ``audience`` 里）：一定读到原话，跟位置、跟渠道都无关。位置
    数据算错了也必须送到——"赤尾对绫奈说了句话"这件事的成立与否，不该取决于世界
    模型有没有把绫奈的位置记对。``audience`` 可以有好几个人，一次说给两个姐妹是
    一件事，不是两条事件。
  * **按位置旁听**（她不在 ``audience`` 里）：按 :mod:`app.living.place` 的三档规则
    裁——同一地点拿原话，同一栋的别处只知道有动静（``content`` 是 ``None``），够
    不着的连这行都看不到。

**旁听判的是"事情发生时她在不在场"，不是"她现在在哪"。** 依据是写入时拍进事件行
的 ``who_was_where`` 快照，读取侧一次位置查询都不做。所以同一条 happening 无论什么
时候被读，裁出来的结果字字一样。按读取时的最新位置判是错的契约：事件可能在她整轮
模型调用期间提交，而她在缝末换了房间，下一缝就会拿新位置去反向裁旧事件——在场的人
漏听、不在场的人反而听见。

**渠道决定的是"旁边的人能不能感知到"，不是行为的优先级。** 当面说的话在同一个屋子
里传得出去；手机和群聊隔着设备，坐在她旁边也看不见那些字。三个 medium 之间没有高低
之分，只有这一条物理差别。

裁剪落在**读取路径本身**而不是渲染层：``Perceived`` 是扁的，只听见动静时
``content`` 就是 ``None``，调用方拿不到被裁掉的原话。放在渲染层裁，等于把"她能
知道什么"这条信息差红线交给下游每个调用方各自守一遍。

游标是 ``seq``（提交序），不是 ``occurred_at``。理由见
:func:`app.living.serial.append_in_commit_order`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app.data.session import get_session
from app.living.place import Reach, reach_between
from app.living.records import MEDIUM_IN_PERSON, Happening
from app.living.serial import append_in_commit_order
from app.living.whereabouts import who_is_where
from app.runtime.migrator import _table_name

_TABLE = _table_name(Happening)

# 一次读多少条原始记录。积压超过这个数就分几次读完（游标每次前进到本批末尾，
# 不丢）。不是"截断上下文"——是一次拿多少行，剩下的下次接着拿。
_DEFAULT_LIMIT = 200


def happening_seq_lock_key(lane: str) -> str:
    """本 lane 上 happening 提交序轴的占用 key（全 lane 一条轴）。"""
    return f"living:seq:happening:{lane}"


@dataclass(frozen=True)
class Perceived:
    """一条被某个人感知到的记录。

    扁平结构，不是"原始行 + 一个 reach 标签"：只听见动静时 ``content`` 就是
    ``None``，调用方没有别的口子拿到原话。

    ``audience`` 是这句话说给谁的（原样带出来，不只是"是不是给我的"）：旁听的人
    要能渲染出"赤尾对绫奈说……"，只给一个 ``directed`` 布尔就丢了"对谁"。
    ``directed`` 是她自己在不在里面——每个调用方都要问的那一句，不让它们各自算。
    """

    seq: int
    happening_id: str
    actor: str
    place: str
    kind: str
    medium: str
    occurred_at: datetime
    audience: tuple[str, ...]
    reach: Reach
    directed: bool
    content: str | None


@dataclass(frozen=True)
class PerceivedWindow:
    """一次读取的结果 + 下次该从哪继续。

    ``next_cursor`` 是本次**扫过的原始记录**的最大 seq（不是过滤后剩下的），所以
    够不着的那些不会每次重扫；一条都没扫到时原样返回传进来的游标。
    """

    items: list[Perceived]
    next_cursor: int


async def record_happening(
    *,
    lane: str,
    happening_id: str,
    actor: str,
    place: str,
    kind: str,
    content: str,
    occurred_at: datetime,
    audience: Sequence[str] = (),
    medium: str = MEDIUM_IN_PERSON,
    channel_id: str | None = None,
) -> Happening:
    """落一件已经发生的事，拿到它在本 lane 提交序上的号。

    写入时拍一张"此刻谁在哪"的快照存进这一行——**这就是"发生时在场"的定义**。
    快照在拿占用之前取：它是这件事发生那一刻的世界状态，不需要跟取号原子。

    ``channel_id`` 只有手机 / 群聊那两个 medium 才有：当面说的话不在任何会话上。

    重放同一个 ``happening_id`` 只落一行，返回库里已有的那一行（快照以第一次
    写入的为准，重放不覆盖——同一件事不该因为重投而换一批听众）。
    """
    snapshot = await who_is_where(lane=lane)
    return await append_in_commit_order(
        Happening,
        stream=happening_seq_lock_key(lane),
        scope={"lane": lane},
        happening_id=happening_id,
        actor=actor,
        place=place,
        kind=kind,
        medium=medium,
        content=content,
        occurred_at=occurred_at,
        audience=list(audience),
        who_was_where=snapshot,
        channel_id=channel_id,
    )


async def _scan(
    *, lane: str, after_seq: int, limit: int
) -> tuple[list[Happening], int]:
    """读 ``seq > after_seq`` 的最早 ``limit`` 条原始记录 + 新游标。"""
    sql = (
        f"SELECT * FROM {_TABLE} "
        f"WHERE lane = :lane AND seq > :after_seq "
        f"ORDER BY seq ASC LIMIT :limit"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "after_seq": after_seq, "limit": limit}
        )
        rows = result.mappings().all()
    items = [
        Happening(**{k: row[k] for k in Happening.model_fields}) for row in rows
    ]
    return items, (items[-1].seq if items else after_seq)


def _perceive(h: Happening, *, persona_id: str) -> Perceived | None:
    """这个人从这条记录里感知到什么；什么都感知不到返回 ``None``。

    纯函数，只看这一行——同一条记录读一百遍结果一样。
    """
    if h.actor == persona_id:
        # 自己说的话 / 自己做的事不回灌给自己（回声）。
        return None

    audience = tuple(h.audience)
    directed = persona_id in audience
    # 事情发生那一刻她在哪；快照里没有她 = 当时定位不到她。
    reach = reach_between(
        observer=h.who_was_where.get(persona_id), happening=h.place
    )

    if directed:
        # 定向：一定拿到原话。reach 照实报（位置可能算错、可能她根本没记过位置），
        # 但**不参与**决定她读不读得到内容。
        content: str | None = h.content
    elif h.medium != MEDIUM_IN_PERSON:
        # 手机 / 群聊：隔着设备，在场也感知不到。不是"优先级低"，是看不见。
        return None
    elif reach is Reach.SAME_PLACE:
        content = h.content
    elif reach is Reach.SAME_BUILDING:
        content = None  # 只知道那边有动静
    else:
        return None

    return Perceived(
        seq=h.seq,
        happening_id=h.happening_id,
        actor=h.actor,
        place=h.place,
        kind=h.kind,
        medium=h.medium,
        occurred_at=h.occurred_at,
        audience=audience,
        reach=reach,
        directed=directed,
        content=content,
    )


async def _read(
    *,
    lane: str,
    persona_id: str,
    after_seq: int,
    limit: int,
    keep_directed: bool,
    keep_overheard: bool,
) -> PerceivedWindow:
    rows, cursor = await _scan(lane=lane, after_seq=after_seq, limit=limit)

    items: list[Perceived] = []
    for h in rows:
        got = _perceive(h, persona_id=persona_id)
        if got is None:
            continue
        if got.directed and not keep_directed:
            continue
        if not got.directed and not keep_overheard:
            continue
        items.append(got)
    return PerceivedWindow(items=items, next_cursor=cursor)


async def read_directed_to(
    *,
    lane: str,
    persona_id: str,
    after_seq: int = 0,
    limit: int = _DEFAULT_LIMIT,
) -> PerceivedWindow:
    """只读直接说给她 / 做给她的，一定带原话，跟她在哪无关。"""
    return await _read(
        lane=lane,
        persona_id=persona_id,
        after_seq=after_seq,
        limit=limit,
        keep_directed=True,
        keep_overheard=False,
    )


async def read_overheard_by(
    *,
    lane: str,
    persona_id: str,
    after_seq: int = 0,
    limit: int = _DEFAULT_LIMIT,
) -> PerceivedWindow:
    """只读旁听到的（不是说给她的），按当时在不在场三档裁。"""
    return await _read(
        lane=lane,
        persona_id=persona_id,
        after_seq=after_seq,
        limit=limit,
        keep_directed=False,
        keep_overheard=True,
    )


async def read_perceived_by(
    *,
    lane: str,
    persona_id: str,
    after_seq: int = 0,
    limit: int = _DEFAULT_LIMIT,
) -> PerceivedWindow:
    """她这一缝感知到的全部（定向 + 旁听），一条游标、按提交序。

    有这个合并入口，是因为一缝只有一个"读到哪了"。两条路径各自带一个游标，迟早
    会出现"定向读到 12、旁听读到 9"这种两个游标各推各的，中间那几条谁也不认领。
    """
    return await _read(
        lane=lane,
        persona_id=persona_id,
        after_seq=after_seq,
        limit=limit,
        keep_directed=True,
        keep_overheard=True,
    )
