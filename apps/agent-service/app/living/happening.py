"""她自己的经历：做过的事、说过的话 —— 写入、读取，和她读到的样子。

每一条都是她自己的：``actor`` 就是她。当面说的、当面做的、手机上发的、去撤回的，都记在
这里，她回看得到（:mod:`app.living.snapshot` 那一段"你刚做过、说过"、日记那一页的材料）。

**这里只记她自己的，也只给她自己读。** 读这张表的每一条查询都按 ``actor`` 筛到一个人
（``tests/living/test_only_her_own_records.py`` 守着）。别人能不能察觉到她做了什么，不由
life 判断：当面做的事一轮一条汇总发给 world，只对姐妹说的话直接发给那位姐妹
（:mod:`app.living.outgoing`）；别人做了什么、说了什么，由 world 判断后告诉她，或者姐妹
直接对她说，都进她的收件箱（:mod:`app.living.received`）。

**渠道是客观事实**（``medium``）：当面说的、当面做的进 world 的汇总；手机和群聊隔着设备，
只在她自己的记录里。

游标是 ``seq``（提交序），不是 ``occurred_at``。理由见
:func:`app.living.serial.append_in_commit_order`。

**一条记录长什么样也归这里**（:func:`own_line`）：同一条记录在她每一轮里和在日记材料里必须
长得一样，两份各写各的必然漂移。放在这里而不是 :mod:`app.living.snapshot`，是因为 snapshot
要读日记那一页，日记反过来 import snapshot 就绕成环。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import text

from app.data.session import get_session
from app.living.records import (
    KIND_SPEECH,
    MEDIUM_IN_PERSON,
    OUTBOUND_HAPPENING_PREFIX,
    Happening,
)
from app.living.serial import append_in_commit_order
from app.runtime.migrator import _table_name

_TABLE = _table_name(Happening)


def happening_seq_lock_key(lane: str) -> str:
    """本 lane 上 happening 提交序轴的占用 key（全 lane 一条轴）。

    三个人写同一张表，取号在这一把占用里排队。读的一侧只读她自己的那几行，按这条轴排先后
    （:mod:`app.living.outgoing` 讲到哪了、日记里她的几件谁先谁后）。
    """
    return f"living:seq:happening:{lane}"


async def record_happening(
    *,
    lane: str,
    happening_id: str,
    actor: str,
    kind: str,
    content: str,
    occurred_at: datetime,
    audience: Sequence[str] = (),
    medium: str = MEDIUM_IN_PERSON,
    channel_id: str | None = None,
) -> Happening:
    """记下她做过的一件事，拿到它在本 lane 提交序上的号。

    ``channel_id`` 只有手机 / 群聊那两个 medium 才有：当面说的话不在任何会话上。

    重放同一个 ``happening_id`` 只落一行，返回库里已有的那一行。
    """
    return await append_in_commit_order(
        Happening,
        stream=happening_seq_lock_key(lane),
        scope={"lane": lane},
        happening_id=happening_id,
        actor=actor,
        kind=kind,
        medium=medium,
        content=content,
        occurred_at=occurred_at,
        audience=list(audience),
        channel_id=channel_id,
    )


async def read_her_happenings_between(
    *, lane: str, persona_id: str, since: datetime, until: datetime
) -> list[Happening]:
    """她自己 ``[since, until)`` 之间做过、说过的事，按提交序升序。

    **开窗按 ``occurred_at``、排序按 ``seq``**，两者各管一件事：一整个生活日的边界是钟点
    （凌晨四点到凌晨四点），而她自己的几件事谁先谁后只有提交序说得清——同一轮里的几件
    ``occurred_at`` 都是那一轮的『现在』，提前来的那一轮先落地、钟点更早的那一轮后落地时，
    钟点的先后跟她做事的先后是反的。

    没有条数上限，因为它答的是"这一天"这个有界的问题——截断意味着某一天的某几个小时静默
    消失，而那正是日记要接住的东西。
    """
    sql = (
        f"SELECT * FROM {_TABLE} "
        f"WHERE lane = :lane AND actor = :actor "
        f"AND occurred_at >= :since AND occurred_at < :until "
        f"ORDER BY seq ASC"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql),
            {"lane": lane, "actor": persona_id, "since": since, "until": until},
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
    （``act``）、她去撤的那句原话，``audience`` 里也是她自己在 ``say`` 的 ``to`` 里写下的
    名字。没有任何一条逐字通道让第三方决定这里的字节。别的参与者写下的字（她收到的消息）
    过，见 :func:`app.living.received.received_line`。给她自己的话套上 ``&quot;`` 是拿她读
    自己记忆的清晰度，换一个这条路上根本不存在的威胁。完整判据写在
    :func:`app.living.records.esc` 上。
    """
    handle = message_handle(h.happening_id)
    tail = f"［{handle}］" if handle is not None else ""
    if h.kind != KIND_SPEECH:
        return f"你 {h.content}{tail}"
    if h.audience:
        return f"你对 {'、'.join(h.audience)} 说：「{h.content}」{tail}"
    return f"你说：「{h.content}」{tail}"
