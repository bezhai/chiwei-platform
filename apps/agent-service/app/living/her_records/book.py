"""她自己的经历和她在哪，摆进她一轮的样子，和别的插件经服务读写它们的那一面（her_records 插件的
插件对象）。

两段由这里摆：

  * **状态段**「你刚做过、说过」（:meth:`OwnRecords.recent_section`）：她自己最近做过、说过的那几条。
    只在这份历史接不住她最近说过的话时才带。
  * **输入段**「手上」（:meth:`OwnRecords.whereabouts_section`）：她在哪、在做什么，每轮都送。

``her_records`` 服务（:class:`app.living.services.HerRecords`）：说话、撤回、她自己那几只手记下的经历
和位置都经它落库；日记读一天的经历、往外讲她做了什么、手机认她上次在哪条会话上开口，也都经它读。
每一条读都按一个人筛（``tests/living/test_only_her_own_records.py`` 守着）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.agent.round import Round
from app.data.persist import fetch_all, fetch_one, fetch_value, from_row
from app.data.schema import table_name
from app.infra.cst_time import dated_clock
from app.living.her_records.deeds import (
    Recorded,
    note_recorded,
    own_line,
    read_her_happenings_between,
    recent_own_happenings,
    record_happening,
)
from app.living.her_records.records import Happening, Whereabouts
from app.living.her_records.whereabouts import current_whereabouts, note_whereabouts
from app.living.services import MEDIUM_IN_PERSON, SinceMarks, WhoLivesHere

_HAPPENING_TABLE = table_name(Happening)
_WHEREABOUTS_TABLE = table_name(Whereabouts)


class OwnRecords:
    """her_records 插件对象：她一轮里的两段，和 ``her_records`` 服务。名字对照从 ``residents`` 服务
    取（她记下的"说给谁"里，姐妹的 id 摆出来是姐妹的名字）。不带状态。"""

    def __init__(self, *, residents: WhoLivesHere) -> None:
        self.residents = residents

    # -- 摆到她眼前的 ------------------------------------------------------------

    async def recent_section(self, round: Round) -> str | None:
        """状态：她自己最近做过、说过的那几条。

        只在这份历史接不住她最近说过的话时才带：一根标记消息都没有（冷启动、清过库、硬顶全裁掉
        了），或者上一轮没存下来（:attr:`app.agent.continuity.HistoryView.continues_unbroken`）。
        判据问的是**这份历史**，不是这一轮要立哪种标记消息 —— 缺口撞上清理点、硬顶之后那一轮，
        立的都是清理那根。接得住就不给：她 4 小时内说过的话都原样在上下文里，再抄一份最近 12 句
        就是同一句话在一轮输入里出现好几遍。
        """
        if round.history is not None and round.history.continues_unbroken:
            return None
        own_recent = await recent_own_happenings(
            lane=round.lane, persona_id=round.participant
        )
        if not own_recent:
            return "你刚做过、说过：（还没有）"
        residents = self.residents.residents()
        lines = [
            f"- {dated_clock(h.occurred_at, now=round.now)} {own_line(h, residents=residents)}"
            for h in own_recent
        ]
        return "你刚做过、说过：\n" + "\n".join(lines)

    async def whereabouts_section(self, round: Round) -> str:
        """输入：她在哪、在做什么。

        **这一段是实况，不是她此刻的样子。** "读一百遍字字一样"这条对日记、挂念、刚做过说过
        成立，对位置不成立：她自己每一轮都可能改（``move_to`` / ``switch_to``），改完到下一个
        清理点之间，重铺那份说的还是旧位置 —— 而她在哪是这一轮里最不能过期的一样（world 按她
        报的位置判断谁看得见她）。
        """
        doing = await current_whereabouts(lane=round.lane, persona_id=round.participant)
        if doing is None:
            return "手上：你还没定下自己在哪、在做什么。"
        return f"手上：你在 {doing.place}，正在 {doing.doing}。"

    # -- her_records 服务 ----------------------------------------------------------

    async def record_happening(
        self,
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
        """记下她做过的一件事（:func:`~app.living.her_records.deeds.record_happening`），并记进这一轮
        她做了几件（:func:`~app.living.her_records.deeds.note_recorded`）。"""
        happening = await record_happening(
            lane=lane,
            happening_id=happening_id,
            actor=actor,
            kind=kind,
            content=content,
            occurred_at=occurred_at,
            audience=audience,
            medium=medium,
            channel_id=channel_id,
        )
        note_recorded(happening_id)
        return happening

    def recorded_this_round(self, round: Round) -> int:
        """这一轮她真的落下了几件事，去重后（落地那一行的 ``recorded``）。"""
        return len(set(round.state(Recorded).happening_ids))

    async def note_whereabouts(
        self,
        *,
        lane: str,
        persona_id: str,
        moment_id: str,
        place: str,
        doing: str,
        noted_at: datetime,
    ) -> Whereabouts:
        """记下她这一轮在哪、在做什么（:func:`~app.living.her_records.whereabouts.note_whereabouts`）。"""
        return await note_whereabouts(
            lane=lane,
            persona_id=persona_id,
            moment_id=moment_id,
            place=place,
            doing=doing,
            noted_at=noted_at,
        )

    async def current_whereabouts(self, *, lane: str, persona_id: str) -> Whereabouts | None:
        """她当前在哪、在做什么（:func:`~app.living.her_records.whereabouts.current_whereabouts`）。"""
        return await current_whereabouts(lane=lane, persona_id=persona_id)

    async def happenings_between(
        self, *, lane: str, persona_id: str, since: datetime, until: datetime
    ) -> list[Happening]:
        """她 ``[since, until)`` 之间做过、说过的事
        （:func:`~app.living.her_records.deeds.read_her_happenings_between`）。"""
        return await read_her_happenings_between(
            lane=lane, persona_id=persona_id, since=since, until=until
        )

    def own_line(self, deed: Happening) -> str:
        """她自己那条经历摆到她眼前的样子（:func:`~app.living.her_records.deeds.own_line`）。"""
        return own_line(deed, residents=self.residents.residents())

    async def marks(self, *, lane: str, persona_id: str) -> tuple[int, int]:
        """她此刻最新的经历号和位置号，没有是 0。"""
        sql = (
            f"SELECT (SELECT COALESCE(MAX(seq), 0) FROM {_HAPPENING_TABLE} "
            f"WHERE lane = :lane AND actor = :persona_id) AS happening_seq, "
            f"(SELECT COALESCE(MAX(seq), 0) FROM {_WHEREABOUTS_TABLE} "
            f"WHERE lane = :lane AND persona_id = :persona_id) AS whereabouts_seq"
        )
        (row,) = await fetch_all(sql, lane=lane, persona_id=persona_id)
        return row["happening_seq"], row["whereabouts_seq"]

    async def after(
        self, *, lane: str, persona_id: str, marks: tuple[int, int]
    ) -> SinceMarks:
        """她自己 ``seq`` 在 ``marks`` 之后的经历和位置，按提交序，连同落库时刻；外加这一段之前她最后
        在哪（没有是 ``None``）。"""
        happening_seq, whereabouts_seq = marks
        deeds_sql = (
            f"SELECT * FROM {_HAPPENING_TABLE} "
            f"WHERE lane = :lane AND actor = :persona_id AND seq > :after ORDER BY seq"
        )
        moves_sql = (
            f"SELECT * FROM {_WHEREABOUTS_TABLE} "
            f"WHERE lane = :lane AND persona_id = :persona_id AND seq > :after ORDER BY seq"
        )
        before_sql = (
            f"SELECT * FROM {_WHEREABOUTS_TABLE} "
            f"WHERE lane = :lane AND persona_id = :persona_id AND seq <= :after "
            f"ORDER BY seq DESC LIMIT 1"
        )
        moves = await fetch_all(moves_sql, lane=lane, persona_id=persona_id, after=whereabouts_seq)
        earlier = await fetch_one(
            before_sql, lane=lane, persona_id=persona_id, after=whereabouts_seq
        )
        deeds = await fetch_all(deeds_sql, lane=lane, persona_id=persona_id, after=happening_seq)
        return SinceMarks(
            deeds=[(row["created_at"], from_row(Happening, row)) for row in deeds],
            moves=[(row["created_at"], from_row(Whereabouts, row)) for row in moves],
            before=None if earlier is None else from_row(Whereabouts, earlier),
        )

    async def last_spoke_in(
        self, *, lane: str, persona_id: str, channel_id: str
    ) -> datetime | None:
        """她上次在这条会话上开口的时刻；从没说过返回 ``None``。

        读的是她自己的经历，不是 ``common_message`` —— 出站消息要等 chat-response-worker 异步落库，
        按它算会把刚说完的话算成"从没说过"。
        """
        sql = (
            f"SELECT MAX(occurred_at) FROM {_HAPPENING_TABLE} "
            f"WHERE lane = :lane AND actor = :persona_id AND channel_id = :channel_id"
        )
        return await fetch_value(sql, lane=lane, persona_id=persona_id, channel_id=channel_id)
