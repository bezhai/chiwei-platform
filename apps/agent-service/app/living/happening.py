"""谁在哪、对谁、通过什么渠道、做了什么说了什么 —— 写入、读取，和她读到的样子。

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
模型调用期间提交，而她在这一轮快结束时换了房间，下一轮就会拿新位置去反向裁旧事件——在场的人
漏听、不在场的人反而听见。

**渠道决定的是"旁边的人能不能感知到"，不是行为的优先级。** 当面说的话在同一个屋子
里传得出去；手机和群聊隔着设备，坐在她旁边也看不见那些字。三个 medium 之间没有高低
之分，只有这一条物理差别。

裁剪落在**读取路径本身**而不是渲染层：``Perceived`` 是扁的，只听见动静时
``content`` 就是 ``None``，调用方拿不到被裁掉的原话。放在渲染层裁，等于把"她能
知道什么"这条信息差红线交给下游每个调用方各自守一遍。

游标是 ``seq``（提交序），不是 ``occurred_at``。理由见
:func:`app.living.serial.append_in_commit_order`。

**一条记录长什么样也归这里**（:func:`own_line` / :func:`perceived_line`）。它本来
住在 :mod:`app.living.snapshot`，只有一个读者的时候放哪都行；:mod:`app.living.day_page`
成为第二个读者之后就不行了——同一条记录在这一轮里和在日记材料里必须长得一样，而两份
各写各的必然漂移。搬到这里而不是反过来让 day_page import snapshot，是因为 snapshot
要读日记那一页，那样绕成环。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app.data.session import get_session
from app.infra.cst_time import dated_clock
from app.living.place import Reach, reach_between
from app.living.records import (
    KIND_SPEECH,
    MEDIUM_IN_PERSON,
    OUTBOUND_HAPPENING_PREFIX,
    WORLD_ACTOR,
    Happening,
    esc,
)
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


def perceive(h: Happening, *, persona_id: str) -> Perceived | None:
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
        got = perceive(h, persona_id=persona_id)
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
    """她这一轮感知到的全部（定向 + 旁听），一条游标、按提交序。

    有这个合并入口，是因为每一轮只有一个"读到哪了"。两条路径各自带一个游标，迟早
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


async def read_happenings_between(
    *, lane: str, since: datetime, until: datetime
) -> list[Happening]:
    """``[since, until)`` 之间发生过的全部原始记录，按提交序升序。

    **开窗按 ``occurred_at``、排序按 ``seq``**，两者各管一件事：一整个生活日的边界
    是钟点（凌晨四点到凌晨四点），而"哪件先哪件后"只有提交序说了算——``occurred_at``
    跨 persona 并发时跟落库顺序无关，拿它排会让同一刻的几条随机换位。

    **不裁给任何人看**：返回的是原始行，谁感知到什么由 :func:`perceive` 另判。

    没有条数上限，因为它答的是"这一天"这个有界的问题——截断意味着某一天的某几个
    小时静默消失，而那正是日记要接住的东西。代价是异常量的一天（补数据、重放）会
    一次读进内存；真撞上再说，不预先加一个会造成静默失明的上限。
    """
    sql = (
        f"SELECT * FROM {_TABLE} "
        f"WHERE lane = :lane AND occurred_at >= :since AND occurred_at < :until "
        f"ORDER BY seq ASC"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "since": since, "until": until}
        )
        rows = result.mappings().all()
    return [
        Happening(**{k: row[k] for k in Happening.model_fields}) for row in rows
    ]


# ---------------------------------------------------------------------------
# 一条记录摆到她眼前的样子
# ---------------------------------------------------------------------------


def message_handle(happening_id: str) -> str | None:
    """这一条要是她发出去的消息，给出她能拿去撤回的那个编号；否则 ``None``。

    编号就是 ``outbound_id``——:func:`app.living.takeback.take_back_message` 按等值查
    的那个键。前缀走 :data:`~app.living.records.OUTBOUND_HAPPENING_PREFIX`，跟
    :mod:`app.living.mouth` 拼 ``happening_id`` 时用的是同一个常量，所以"这里印出去
    的"和"她照抄回来的"必然是同一个东西。

    **判据是前缀，不是** ``kind``：当面说的话和发消息的 ``kind`` 都是 ``speech``，
    但当面说的话撤不了。给它一个编号就是给她一个指了会失败的东西。
    """
    if not happening_id.startswith(OUTBOUND_HAPPENING_PREFIX):
        return None
    return happening_id[len(OUTBOUND_HAPPENING_PREFIX):]


def own_line(h: Happening) -> str:
    """她自己那条记录的样子；发出去的消息末尾带上它的编号。

    带编号是**给她一个消息级句柄**。没有它的时候她只能拿原话去指要撤哪一条，于是
    后端得在逻辑层按内容猜——同一句话说过两遍就分不出是哪一次。真人撤消息是看着那条
    点的，句柄一直都在他手上；她的句柄就印在这里。

    整串照印，不截断：截断要配一套前缀唯一性校验，而那个分支在真实数据量下永远不会
    触发。她是模型，照抄一串字符没有负担。

    **``content`` 不过 :func:`app.living.records.esc`，这是有意的。** 这一段印的全是
    她自己这一侧的模型写下的字：她说的话（``say`` / 嘴发出去的那句）、她做的事
    （``act``）、她去撤的那句原话。没有任何一条逐字通道让第三方决定这里的字节 ——
    :func:`perceived_line` 那边有（外面那五样是外部数据源逐字交回来的），所以那边过。
    给她自己的话套上 ``&quot;`` 是拿她读自己记忆的清晰度，换一个这条路上根本不存在
    的威胁。完整判据写在 :func:`app.living.records.esc` 上。
    """
    handle = message_handle(h.happening_id)
    tail = f"［{handle}］" if handle is not None else ""
    if h.kind != KIND_SPEECH:
        return f"你 {h.content}{tail}"
    if h.audience:
        return f"你对 {'、'.join(h.audience)} 说：「{h.content}」{tail}"
    return f"你说：「{h.content}」{tail}"


def perceived_line(p: Perceived, *, me: str) -> str:
    """一条感知记录的样子。``content is None`` 时**没有任何口子**能漏出原话。

    ``content`` 过 :func:`app.living.records.esc`，:func:`own_line` 不过。差别不在
    "谁写的更可信"，在**有没有一条逐字通道**：这一条上有 ——
    :func:`app.living.outside.look_outside` 把天气、番名从外部数据源**逐字**拼进
    ``content``，那几个源在外面，交回来什么就是什么。``own_line`` 那边一条都没有
    （她自己的话、她自己的动作、她去撤的那句原话，全是她这一侧的模型写的）。判据
    写在 :func:`app.living.records.esc` 上。

    ``actor`` / ``place`` / ``audience`` 不过：它们是 persona id、世界的地点路径，
    取值由代码定死，外面写不进来。
    """
    if p.content is None:
        return f"{p.place} 那边有动静"
    content = esc(p.content)
    if p.actor == WORLD_ACTOR:
        # 世界自己发生的事（天黑、快递到）没有"谁"，加个主语就是在编人。
        return content
    if p.kind != KIND_SPEECH:
        return f"{p.actor} {content}" + ("（是冲着你来的）" if p.directed else "")
    if p.directed:
        # 一句话可以同时说给两个人；只说"对你说"会让她看不见姐姐也在场。
        others = [a for a in p.audience if a != me]
        also = f"和 {'、'.join(others)} " if others else ""
        return f"{p.actor} 对你{also}说：「{content}」"
    if p.audience:
        return f"{p.actor} 对 {'、'.join(p.audience)} 说：「{content}」"
    return f"{p.actor} 说：「{content}」"


def happening_line(h: Happening, *, me: str, now: datetime) -> str | None:
    """一条原始记录在 ``me`` 眼里的一整行（带时刻）；她感知不到就 ``None``。

    **她自己做的走 :func:`own_line`，别人的先过 :func:`perceive`。** 少了前半句就是
    把回声抑制照抄进来——她的一天里只剩别人做的事，自己那些一件不剩
    （:func:`read_perceived_by` 丢掉 ``actor == persona_id``，那对这一轮是对的，对
    "回看这一整天"是致命的）。

    时刻走 :func:`app.infra.cst_time.dated_clock`：同一个日历日给 ``HH:MM CST``，
    跨天补 ``MM-DD``。一个生活日跨两个日历日（04:00 到次日 04:00），凌晨那几行只给
    时分的话读起来像"这天很早"。
    """
    if h.actor == me:
        said = own_line(h)
    else:
        got = perceive(h, persona_id=me)
        if got is None:
            return None
        said = perceived_line(got, me=me)
    return f"{dated_clock(h.occurred_at, now=now)} {said}"
