"""她自己的经历：做过的事、说过的话 —— 写入、读取，和她读到的样子。

每一条都是她自己的：``actor`` 就是她。当面说的、当面做的、手机上发的、去撤回的，都记在
这里，她回看得到（她一轮里"你刚做过、说过"那一段、日记那一页的材料）。

**这里只记她自己的，也只给她自己读。** 读这张表的每一条查询都按 ``actor`` 筛到一个人
（``tests/living/test_only_her_own_records.py`` 守着）。别人能不能察觉到她做了什么，不由
life 判断：当面做的事一轮一条汇总发给 world，只对姐妹说的话直接发给那位姐妹
（:mod:`app.living.household.outgoing`）；别人做了什么、说了什么，由 world 判断后告诉她，或者姐妹
直接对她说，都进她的收件箱（:mod:`app.living.household.received`）。

**渠道是客观事实**（``medium``）：当面说的、当面做的进 world 的汇总；手机和群聊隔着设备，
只在她自己的记录里。

游标是 ``seq``（提交序），不是 ``occurred_at``。理由见 :class:`app.living.her_records.records.Happening`，
号由 :func:`app.data.ordered.insert_in_commit_order` 分配。

**这一轮她做了几件事也记在这里**（:class:`Recorded`，她这一轮的状态）：她的工具每真的落下一件事
就记一笔（:func:`note_recorded`），这一轮落地时数。

**一条记录长什么样也归这里**（:func:`own_line`）：同一条记录在她每一轮里和在日记材料里必须
长得一样，两份各写各的必然漂移；日记经 ``her_records`` 服务拿到的就是这一份。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.agent.round import current_round
from app.data.ordered import insert_in_commit_order
from app.data.persist import fetch_all, from_row
from app.data.schema import table_name
from app.living.common import printed_handle
from app.living.her_records.records import Happening
from app.living.services import KIND_SPEECH, MEDIUM_IN_PERSON, Residents

# 她自己最近做过 / 说过的多少条。按**条数**而不是按时间窗：安静一整天的时候，她
# 上一次开口仍然读得到；而热闹的时候也不会把半天的行为一次全灌进来。
#
# 12 的量级依据：她真正动手 / 开口的轮次远少于"继续"的轮次，12 条大致覆盖她最近几个
# 小时的行为轨迹 —— 足够让"刚答应姐姐的事"活到她下一次换事情、把它列进心上为止。
#
# 这一段只在上下文接不住她最近说过的话时给（上下文是空的、硬顶把标记消息全裁掉了、上一
# 轮没存下来）。上下文不按天清零，所以正常跑着的时候它不出现；出现的那一轮，这 12 条就
# 是她眼前唯一的说话样本，之前的腔调会跟着带进来。这是有意保留的：她得记得自己刚说过
# 什么。
OWN_RECENT_LIMIT = 12

_TABLE = table_name(Happening)


@dataclass
class Recorded:
    """这一轮她真的落库的事（``happening_id``，去重后就是"她这一轮说 / 做了几件事"）。当面说的、
    手机上发出去的都算——对她自己来说都是"我刚才说了这句话"。这一轮的状态
    （``current_round().state(Recorded)``），由这一轮落地时数。"""

    happening_ids: list[str] = field(default_factory=list)


def note_recorded(happening_id: str) -> None:
    """记下这一轮她又做了一件事（当面说的、手机上发的，一视同仁）。"""
    current_round().state(Recorded).happening_ids.append(happening_id)


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
    return await insert_in_commit_order(
        Happening(
            lane=lane,
            happening_id=happening_id,
            actor=actor,
            kind=kind,
            medium=medium,
            content=content,
            occurred_at=occurred_at,
            audience=list(audience),
            channel_id=channel_id,
        )
    )


async def read_her_happenings_between(
    *, lane: str, persona_id: str, since: datetime, until: datetime
) -> list[Happening]:
    """她自己 ``[since, until)`` 之间做过、说过的事，按提交序升序。

    **开窗按 ``occurred_at``、排序按 ``seq``**，两者各管一件事：一整个生活日的边界是钟点
    （凌晨四点到凌晨四点），而她自己的几件事谁先谁后只有提交序说得清——同一轮里的几件
    ``occurred_at`` 都是那一轮的『现在』，分不出先后；常规那一轮还记着格子的旧记录里，提前来的
    那一轮先落地、钟点更早的那一轮后落地时，钟点的先后跟她做事的先后是反的。

    没有条数上限，因为它答的是"这一天"这个有界的问题——截断意味着某一天的某几个小时静默
    消失，而那正是日记要接住的东西。
    """
    sql = (
        f"SELECT * FROM {_TABLE} "
        f"WHERE lane = :lane AND actor = :actor "
        f"AND occurred_at >= :since AND occurred_at < :until "
        f"ORDER BY seq ASC"
    )
    rows = await fetch_all(sql, lane=lane, actor=persona_id, since=since, until=until)
    return [from_row(Happening, row) for row in rows]



async def recent_own_happenings(
    *, lane: str, persona_id: str, limit: int = OWN_RECENT_LIMIT
) -> list[Happening]:
    """她自己最近做过 / 说过的若干条，按发生先后升序（最近的在最后）。

    按 ``seq`` 取最近的一段再翻过来：``occurred_at`` 跨 persona 并发时跟落库顺序
    无关，按它排会让同一刻的几条随机换位（见 :class:`app.living.her_records.records.Happening`
    的 ``seq``）。
    """
    sql = (
        f"SELECT * FROM {_TABLE} "
        f"WHERE lane = :lane AND actor = :actor "
        f"ORDER BY seq DESC LIMIT :limit"
    )
    rows = await fetch_all(sql, lane=lane, actor=persona_id, limit=limit)
    items = [from_row(Happening, row) for row in rows]
    items.reverse()
    return items

# ---------------------------------------------------------------------------
# 一条记录摆到她眼前的样子
# ---------------------------------------------------------------------------


def own_line(h: Happening, *, residents: Residents) -> str:
    """她自己那条记录的样子；发出去的消息末尾带上它的编号。

    带编号是**给她一个消息级句柄**。没有它的时候她只能拿原话去指要撤哪一条，于是
    后端得在逻辑层按内容猜——同一句话说过两遍就分不出是哪一次。真人撤消息是看着那条
    点的，句柄一直都在他手上；她的句柄就印在这里。

    整串照印，不截断：截断要配一套前缀唯一性校验，而那个分支在真实数据量下永远不会
    触发。她是模型，照抄一串字符没有负担。

    **``content`` 不过 :func:`app.living.common.esc`，这是有意的。** 这一段印的全是
    她自己这一侧的模型写下的字：她说的话（``say`` / 嘴发出去的那句）、她做的事
    （``act``）、她去撤的那句原话，``audience`` 里也是她自己在 ``say`` 的 ``to`` 里写下的
    名字（三姐妹的 id 换成她们的名字，见 :meth:`app.living.services.Residents.names_for`）。
    没有任何一条逐字通道让第三方决定这里的字节。别的参与者写下的字（她收到的消息）
    过，见 :func:`app.living.household.received.received_line`。给她自己的话套上 ``&quot;`` 是拿她读
    自己记忆的清晰度，换一个这条路上根本不存在的威胁。完整判据写在
    :func:`app.living.common.esc` 上。
    """
    tail = printed_handle(h.happening_id)
    if h.kind != KIND_SPEECH:
        return f"你 {h.content}{tail}"
    if h.audience:
        to = "、".join(residents.names_for(h.audience))
        return f"你对 {to} 说：「{h.content}」{tail}"
    return f"你说：「{h.content}」{tail}"
