"""她做的事发出去：一段经历一条汇总给 world，只对姐妹说的话直接给那位姐妹。

**给 world 的是她在世界里当面做的事**：换在做的事、换地方（``switch_to`` / ``move_to``）、动作
（``act``）、不是只对姐妹说的话（``say`` 对别人说的、不冲着谁说的）。按先后排，用她自己的原话，
最后一句是她这时候在哪、在做什么——world 判断谁会察觉，靠的是各人自己报的位置。一段最多一条：
world 一次只处理一条消息，每条都叫醒它跑完整的一轮，一个动作一条的话，一轮里的三个动作就是
world 的三轮，每一轮只看到一部分。她这一段在世界里什么都没做，就不发。

手机上发的消息、撤回消息照常记在她的经历里（:mod:`app.living.mouth`、:mod:`app.living.takeback`），
她自己回看得到，但隔着设备，不进汇总：判据是经历上的 ``medium``，不是这一轮记下了哪些
（:data:`app.living.scope.FEATURE_RECORDED` 里两种都有）。

**只对姐妹说的话直接发给姐妹**，一位一条，不经过 world。同时还对别人说的，姐妹照样收到直达的
那条，这句话也进汇总，别人听没听见由 world 判断。姐妹按她们在世界里的名字认
（:meth:`app.living.participants.Residents.sisters_in`）；不是姐妹名字的，一律是世界里的某个人。

**汇总从她存下的经历里取，不从这一轮的内存里取**（:class:`~app.living.records.Happening`、
:class:`~app.living.records.Whereabouts`）。所以这一轮失败、超时、赶上部署被打断，她已经做了的
事也还在，下一次照样发出去。

**一段经历变成要发的消息，和"讲到哪了"在同一个事务里落地**（:func:`compose`、
:class:`OutgoingUpTo`）。"讲到哪了"是两条轴上的 ``seq``：经历是全泳道一条提交序，位置是她自己
一条。她的经历只在她自己的那一轮里写（占着她的 moment 占用），这里也只在占用里调，所以读的那
一刻她的经历全都落了库，取到的最大号之前不会再冒出一条。事务落地之前进程没了，两样都不留，
下一次从同一段经历重新生成，id 和正文跟没落地的那一次一样：

  * 对姐妹的那条，id 由那句话的经历 id 和收件人决定，正文是那句原话；
  * 给 world 的那条，id 由这一段经历的起止决定，正文由这一段存下的内容决定。

**id 和正文落地之后就定死在那一行上**（:class:`OutgoingMessage`），重发时原样再发，对方按 id
去重。

**发**（:func:`send_unsent`）：还没有结果的逐条发，发完记一条结果（:class:`OutgoingResult`）。
没确认（通信机制抛错、进程没了、记结果失败）就什么都不记，下一次原样再发；对方没开收件箱是
一个确定的结果，记下来，不再发。一位收件人失败不影响别的收件人；同一位收件人前一条没发出去，
后一条等它——她那边按消息到的时刻排，后一句先到就倒了。

**什么时候发**：她每一轮开始前、结束时（:func:`app.living.moment.run_moment_held`）。开始前那
一次接住上一轮没发完的（失败、超时、进程没了）；结束时那一次发这一轮做的。都在她的 moment
占用里。

**开始往外讲之前的经历不补发**：第一次调的时候她还没有"讲到哪了"，就从她那时最新的经历讲起。
那些事发生的时候没有人在听，现在补成一条消息，world 会当成刚发生的事。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from sqlalchemy import text

from app.data.session import get_session
from app.infra.cst_time import dated_clock
from app.living.participants import WORLD, residents
from app.living.records import KIND_SPEECH, MEDIUM_IN_PERSON, Happening, Whereabouts
from app.messaging.sending import send
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_append

logger = logging.getLogger(__name__)

# 派生消息 id 的命名空间，随手换会让没发完的消息和发过的对不上。
_ID_NS = uuid.UUID("5b0e2d71-9a4c-4f38-b6e1-7c2d94a0f153")

# 给 world 的那条的开头和结尾。
_DIGEST_HEAD = "我做了这些（按先后）："


class OutgoingMessage(Data):
    """她要发出去的一条消息。从这一行写下起，id、发给谁、正文都不再变，重发时原样再发。

    自然键 ``(lane, message_id)``。纯 append：发没发出去是另一件事（:class:`OutgoingResult`）。

    ``seq`` 是这条在她要发的消息里的先后（每人一条轴，在占用里取号）：同一位收件人的几条按它
    的先后发。同一个事务里写下的几条 ``created_at`` 一样，分不出先后。
    """

    lane: Annotated[str, Key]
    message_id: Annotated[str, Key]
    persona_id: str
    seq: int
    sender: str
    recipient: str
    body: str

    class Meta:
        # 读侧唯一形状：这个人还没有结果的那些，按先后。
        indexes = (("lane", "persona_id", "seq"),)


class OutgoingResult(Data):
    """一条消息发出去的结果：送到了，或者对方没开收件箱（``reason`` 说为什么）。

    自然键 ``(lane, message_id)``：一条消息只有一个结果。有这一行就不再发。没确认的那一次
    什么都不记：没确认不是结果，可能没到，也可能到了，原样再发一次是安全的。
    """

    lane: Annotated[str, Key]
    message_id: Annotated[str, Key]
    delivered: bool
    reason: str  # 没送到的原因；送到了是 ""

    class Meta:
        # 读侧唯一形状：某一条有没有结果（:func:`unsent` 的 NOT EXISTS）。
        indexes = (("lane", "message_id"),)


class OutgoingUpTo(Data):
    """她的经历讲到哪了：两条轴上到这个号为止的，都已经变成了要发的消息（或者里面没有要发的）。

    自然键是整行：每讲一段落一行，纯 append。两个号都只增不减，最新一行就是号最大的那一行。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    happening_seq: Annotated[int, Key]
    whereabouts_seq: Annotated[int, Key]

    class Meta:
        # 读侧唯一形状：这个人最新的那一行。
        indexes = (("lane", "persona_id", "happening_seq", "whereabouts_seq"),)


_MESSAGE_TABLE = _table_name(OutgoingMessage)
_RESULT_TABLE = _table_name(OutgoingResult)
_UP_TO_TABLE = _table_name(OutgoingUpTo)
_HAPPENING_TABLE = _table_name(Happening)
_WHEREABOUTS_TABLE = _table_name(Whereabouts)


def _derive(*parts: str) -> str:
    return uuid.uuid5(_ID_NS, "\x1f".join(parts)).hex


async def send_what_she_did(*, lane: str, persona_id: str) -> None:
    """把她经历里还没讲出去的那一段变成要发的消息，再把还没有结果的逐条发出去。

    **调用方必须占着她的 moment 占用**（:func:`app.living.moment.life_moment_lock_key`）：讲到哪
    了靠的是"她的经历这一刻全都落了库"，两处同时讲会把同一段讲两遍。
    """
    await compose(lane=lane, persona_id=persona_id)
    await send_unsent(lane=lane, persona_id=persona_id)


async def compose(*, lane: str, persona_id: str) -> list[OutgoingMessage]:
    """她上次讲到的地方之后的经历，变成要发的消息；跟新的"讲到哪了"一个事务落地。交回新写下的。

    还没讲过的话，只记下从她现在最新的经历讲起，什么都不生成（见模块说明最后一段）。
    """
    mark = await _latest_mark(lane=lane, persona_id=persona_id)
    if mark is None:
        await insert_append(await _starting_mark(lane=lane, persona_id=persona_id))
        return []
    moves, before = await _her_whereabouts_after(
        lane=lane, persona_id=persona_id, after=mark.whereabouts_seq
    )
    deeds = await _her_happenings_after(
        lane=lane, persona_id=persona_id, after=mark.happening_seq
    )
    if not moves and not deeds:
        return []

    known = residents()
    sender = known.by_persona[persona_id]
    seq = await _her_last_message_seq(lane=lane, persona_id=persona_id)
    messages: list[OutgoingMessage] = []
    # (落库的先后, 同一刻时位置在前, 发生的时刻, 那一行)。两张表的 seq 不在一条轴上，跨表的
    # 先后只有落库时刻说得清：她的手是一只一只用的，每一只都是自己的事务。
    lines: list[tuple[datetime, int, datetime, str]] = []

    where = before
    for created_at, now in moves:
        line = _change_line(now, before=where)
        where = now
        if line is not None:
            lines.append((created_at, 0, now.noted_at, line))

    for created_at, deed in deeds:
        if deed.medium != MEDIUM_IN_PERSON:
            continue  # 手机上的事，隔着设备
        if deed.kind != KIND_SPEECH:
            lines.append((created_at, 1, deed.occurred_at, deed.content))
            continue
        sisters = known.sisters_in(deed.audience, speaker=persona_id)
        for name in sisters:
            seq += 1
            messages.append(
                OutgoingMessage(
                    lane=lane,
                    message_id=_derive(lane, deed.happening_id, name),
                    persona_id=persona_id,
                    seq=seq,
                    sender=sender,
                    recipient=name,
                    body=_said_to_her(deed, name=name),
                )
            )
        if not deed.audience or any(name not in sisters for name in deed.audience):
            lines.append((created_at, 1, deed.occurred_at, _speech_line(deed)))

    up_to = OutgoingUpTo(
        lane=lane,
        persona_id=persona_id,
        happening_seq=deeds[-1][1].seq if deeds else mark.happening_seq,
        whereabouts_seq=moves[-1][1].seq if moves else mark.whereabouts_seq,
    )
    if lines:
        # 当面做的事都要先有位置（``say`` / ``act`` 没有位置就拒绝），所以有一行就有她在哪。
        assert where is not None, f"{persona_id} 当面做了事，却从没记下过自己在哪"
        seq += 1
        messages.append(
            OutgoingMessage(
                lane=lane,
                message_id=_derive(
                    lane,
                    persona_id,
                    WORLD,
                    f"{mark.happening_seq}-{mark.whereabouts_seq}",
                    f"{up_to.happening_seq}-{up_to.whereabouts_seq}",
                ),
                persona_id=persona_id,
                seq=seq,
                sender=sender,
                recipient=WORLD,
                body=_digest(lines, where=where),
            )
        )

    async with get_session() as s:
        for message in messages:
            await insert_append(message, session=s)
        await insert_append(up_to, session=s)
    return messages


async def send_unsent(*, lane: str, persona_id: str) -> None:
    """还没有结果的逐条发出去，发完一条记一条结果。

    一条没确认就记一行 WARNING、留着下一次再发，接着发别人的；同一位收件人后面的几条这一次先
    不发，等前面那条。不往外抛：这一次没发出去的，下一次一定再试。
    """
    waiting: set[str] = set()
    for message in await unsent(lane=lane, persona_id=persona_id):
        if message.recipient in waiting:
            continue
        try:
            delivery = await send(
                sender=message.sender,
                recipient=message.recipient,
                body=message.body,
                message_id=message.message_id,
            )
            await insert_append(
                OutgoingResult(
                    lane=lane,
                    message_id=message.message_id,
                    delivered=delivery.delivered,
                    reason=delivery.reason or "",
                )
            )
        except Exception:
            waiting.add(message.recipient)
            logger.warning(
                "living outgoing lane=%s persona=%s 发给 %s 的 %s 没确认，下一次原样再发",
                lane,
                persona_id,
                message.recipient,
                message.message_id,
                exc_info=True,
            )
            continue
        if not delivery.delivered:
            logger.warning(
                "living outgoing lane=%s persona=%s 发给 %s 的 %s 没送到（%s），不再发",
                lane,
                persona_id,
                message.recipient,
                message.message_id,
                delivery.reason,
            )


async def unsent(*, lane: str, persona_id: str) -> list[OutgoingMessage]:
    """她要发、还没有结果的那些，按先后。"""
    sql = (
        f"SELECT m.* FROM {_MESSAGE_TABLE} m "
        f"WHERE m.lane = :lane AND m.persona_id = :persona_id "
        f"AND NOT EXISTS (SELECT 1 FROM {_RESULT_TABLE} r "
        f"WHERE r.lane = m.lane AND r.message_id = m.message_id) "
        f"ORDER BY m.seq"
    )
    return [
        OutgoingMessage(**{k: row[k] for k in OutgoingMessage.model_fields})
        for row in await _rows(sql, lane=lane, persona_id=persona_id)
    ]


# ---------------------------------------------------------------------------
# 正文
# ---------------------------------------------------------------------------


def _change_line(now: Whereabouts, *, before: Whereabouts | None) -> str | None:
    """她换了在做的事、或者挪了地方；跟上一条比什么都没变就没有这一行。"""
    if before is None or now.doing != before.doing:
        return f"改做 {now.doing}，在 {now.place}"
    if now.place != before.place:
        return f"挪到 {now.place}"
    return None


def _speech_line(deed: Happening) -> str:
    if deed.audience:
        return f"对 {'、'.join(deed.audience)} 说：「{deed.content}」"
    return f"说：「{deed.content}」"


def _said_to_her(deed: Happening, *, name: str) -> str:
    """给被说的那位姐妹的那条：当面说的，原话，还对谁说了也写上。"""
    others = [n for n in dict.fromkeys(deed.audience) if n != name]
    also = f"和 {'、'.join(others)} " if others else ""
    return f"当面对你{also}说：「{deed.content}」"


def _digest(lines: list[tuple[datetime, int, datetime, str]], *, where: Whereabouts) -> str:
    """给 world 的那条：一行一件，带着发生的时刻；最后是她这时候在哪、在做什么。

    时刻相对这一段里最晚的那一刻渲染（跨了日历日的才带日子），不取发送那一刻：正文要在重发时
    一字不差。
    """
    lines = sorted(lines, key=lambda item: (item[0], item[1]))
    latest = max(at for _, _, at, _ in lines)
    return "\n".join(
        [
            _DIGEST_HEAD,
            *(f"- {dated_clock(at, now=latest)} {line}" for _, _, at, line in lines),
            f"做完这些，我在 {where.place}，正在 {where.doing}。",
        ]
    )


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


async def _latest_mark(*, lane: str, persona_id: str) -> OutgoingUpTo | None:
    sql = (
        f"SELECT * FROM {_UP_TO_TABLE} WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY happening_seq DESC, whereabouts_seq DESC LIMIT 1"
    )
    rows = await _rows(sql, lane=lane, persona_id=persona_id)
    if not rows:
        return None
    return OutgoingUpTo(**{k: rows[0][k] for k in OutgoingUpTo.model_fields})


async def _starting_mark(*, lane: str, persona_id: str) -> OutgoingUpTo:
    """从她现在最新的经历讲起。"""
    sql = (
        f"SELECT (SELECT COALESCE(MAX(seq), 0) FROM {_HAPPENING_TABLE} "
        f"WHERE lane = :lane AND actor = :persona_id) AS happening_seq, "
        f"(SELECT COALESCE(MAX(seq), 0) FROM {_WHEREABOUTS_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id) AS whereabouts_seq"
    )
    (row,) = await _rows(sql, lane=lane, persona_id=persona_id)
    return OutgoingUpTo(lane=lane, persona_id=persona_id, **row)


async def _her_happenings_after(
    *, lane: str, persona_id: str, after: int
) -> list[tuple[datetime, Happening]]:
    """她自己 ``seq > after`` 的经历，按提交序，连同落库时刻。"""
    sql = (
        f"SELECT * FROM {_HAPPENING_TABLE} "
        f"WHERE lane = :lane AND actor = :persona_id AND seq > :after ORDER BY seq"
    )
    rows = await _rows(sql, lane=lane, persona_id=persona_id, after=after)
    return [
        (row["created_at"], Happening(**{k: row[k] for k in Happening.model_fields}))
        for row in rows
    ]


async def _her_whereabouts_after(
    *, lane: str, persona_id: str, after: int
) -> tuple[list[tuple[datetime, Whereabouts]], Whereabouts | None]:
    """她 ``seq > after`` 的位置，按先后，连同落库时刻；外加这一段之前她最后在哪（没有是 ``None``）。"""
    after_sql = (
        f"SELECT * FROM {_WHEREABOUTS_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id AND seq > :after ORDER BY seq"
    )
    before_sql = (
        f"SELECT * FROM {_WHEREABOUTS_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id AND seq <= :after "
        f"ORDER BY seq DESC LIMIT 1"
    )
    rows = await _rows(after_sql, lane=lane, persona_id=persona_id, after=after)
    earlier = await _rows(before_sql, lane=lane, persona_id=persona_id, after=after)

    def whereabouts(row: Any) -> Whereabouts:
        return Whereabouts(**{k: row[k] for k in Whereabouts.model_fields})

    return (
        [(row["created_at"], whereabouts(row)) for row in rows],
        whereabouts(earlier[0]) if earlier else None,
    )


async def _her_last_message_seq(*, lane: str, persona_id: str) -> int:
    sql = (
        f"SELECT COALESCE(MAX(seq), 0) AS seq FROM {_MESSAGE_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id"
    )
    (row,) = await _rows(sql, lane=lane, persona_id=persona_id)
    return int(row["seq"])


async def _rows(sql: str, **params: Any) -> list[Any]:
    async with get_session() as s:
        return list((await s.execute(text(sql), params)).mappings().all())
