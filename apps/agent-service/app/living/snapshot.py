"""她进入一缝时读到的东西 —— 状态快照，不是历史回放。

**为什么不套 ``SessionTranscript``。** 那个有 200 条 / 256KiB 硬上限、每 100 条触发
一次模型折叠。她一天 144 缝，两天就撞顶，而且折叠频率跟着缝的密度走：缝越密、折叠
越频繁、失真越快。更根本的是折叠这个动作本身——它把"发生过什么"压成一段概括，压完
之后原文没了，压错了没人知道。

**换成状态快照。** 她读到的是**此刻的事实**，不是对历史的压缩：

  ==================  ==========================  ==============================
  层                  从哪读                      界从哪来
  ==================  ==========================  ==============================
  手上正在做的事      最新一条 ``Whereabouts``    1 行（"当前"只有一个）
  上一次写下的那天    ``read_day_page_before``    1 页（严格早于当前生活日的最新一页）
  挂着没了结的事      还开着的 ``LooseEnd``       她自己列多少就是多少
  她刚做过 / 说过     她自己的 ``Happening``      最近 N 条
  这段时间感知到的    ``read_perceived_by``       一条游标 + 每缝的条数上限
  ==================  ==========================  ==============================

**为什么这么长不会失真**：五层没有一层是"机器对历史的概括"。头两层是当下状态，读一
百遍字字一样；后两层是原文照搬的最近若干条，只是**少**，不是**歪**。失真来自压缩，
这里一处压缩都没有。会被遗忘的只有第四层滚出窗口的那些——而第三层正是她把重要的东
西从滚动窗口里救出来的那只手，救不救是她的决定（见 :mod:`app.living.loose_ends`）。

**日记那一层是她自己写的，不是折叠出来的。** 这是它跟被否掉的 ``SessionTranscript``
唯一但决定性的区别：一条原始记录都没被动过，那一页是她另写的一份东西（见
:mod:`app.living.day_page`）。少了它她跨不过一天——上面四层全是"当下"，滚出窗口的
东西没有任何一层接得住。这里只负责读和摆，"读哪一页"那条严格早于当前生活日的判据在
:func:`~app.living.day_page.read_day_page_before` 里。

**"她刚做过、说过"那层为什么必须单独存在**：:func:`~app.living.happening.read_perceived_by` 抑制
回声（``actor == persona_id`` 直接丢），所以她从感知那条路**看不见自己刚说过什么**。
少了这一层，她上一缝答应姐姐的话下一缝就凭空消失，"接得上昨天"永远无从谈起。

**裁剪不在这里重做。** 谁感知得到什么由 T1 的读取路径说了算；这里只负责把已经裁好
的东西摆成她读得懂的样子。只听见动静的那条 ``content`` 本来就是 ``None``，渲染层
再怎么写也漏不出原话。

唯一一处在这里**算**出来的东西是线头那层的「到点了 / 还没到」：她给线头挂的时刻当场跟
``now`` 比，库里没有这个状态（:func:`_open_end_line`）。这不是压缩，是把同一个事实
换算成她读得懂的说法——比她自己拿第一行的钟点去减更不容易错，而"这件事算不算了结"
仍然只有她能答。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text

from app.data.session import get_session
from app.infra.cst_time import dated_clock, to_cst_full
from app.living.day_page import LivingDayPage, living_day_of, read_day_page_before
from app.living.happening import (
    PerceivedWindow,
    own_line,
    perceived_line,
    read_perceived_by,
)
from app.living.loose_ends import LooseEnd, format_entry, list_open_loose_ends
from app.living.records import Happening, Whereabouts
from app.living.whereabouts import current_whereabouts
from app.runtime.migrator import _table_name

# 她自己最近做过 / 说过的多少条。按**条数**而不是按时间窗：安静一整天的时候，她
# 上一次开口仍然读得到；而热闹的时候也不会把半天的行为一次全灌进来。
#
# 12 的量级依据：她真正动手 / 开口的缝远少于"继续"的缝，12 条大致覆盖她最近几个
# 小时的行为轨迹 —— 足够让"刚答应姐姐的事"活到她下一次换事情、把它列进心上为止。
OWN_RECENT_LIMIT = 12

# 一缝最多读多少条感知记录。不是截断上下文：游标推到本次扫过的最大 seq，剩下的
# 下一缝接着拿（见 ``PerceivedWindow``）。60 条约等于半小时的动静，积压时几缝就
# 追平。
PERCEIVED_LIMIT = 60

_HAPPENING_TABLE = _table_name(Happening)
_WHEREABOUTS_TABLE = _table_name(Whereabouts)


@dataclass(frozen=True)
class MomentSnapshot:
    """她这一缝读到的全部。

    ``perceived`` 原样带着 :class:`~app.living.happening.PerceivedWindow`，因为
    调用方要拿 ``next_cursor`` 续接下一缝——把游标拆出去传会让"读到哪了"变成两个
    地方各记一份。
    """

    lane: str
    persona_id: str
    now: datetime
    doing: Whereabouts | None
    day_page: LivingDayPage | None
    open_ends: list[LooseEnd]
    own_recent: list[Happening]
    perceived: PerceivedWindow

    def render(self) -> str:
        """摆成她读得懂的样子。每段空的时候如实说空，不留白洞。"""
        return "\n\n".join(
            (
                self._render_now(),
                self._render_hands(),
                self._render_day_page(),
                self._render_open_ends(),
                self._render_own_recent(),
                self._render_perceived(),
            )
        )

    # -- 各段 ------------------------------------------------------------

    def _render_now(self) -> str:
        # 完整口径（年月日 + 星期），不是裸时分：下面几段跨天的行渲染成 ``07-24 23:41
        # CST``，而这一缝喂给她的全部输入就是快照 + 信封（``app.living.moment``），
        # 没有第二个地方说今天几号 —— 不说的话 ``07-24`` 是昨天还是上个月她算不出来，
        # 记日程 / 算 ``remind_at`` 时更是只能瞎填日期分量。
        return f"现在 {to_cst_full(self.now.isoformat())}。"

    def _render_hands(self) -> str:
        if self.doing is None:
            return "手上：你还没定下自己在哪、在做什么。"
        return f"手上：你在 {self.doing.place}，正在 {self.doing.doing}。"

    def _render_day_page(self) -> str:
        """她上一次写下的那一天。摆在「手上」之后、当下那三段之前：它是背景，不是
        此刻正在发生的事。

        **日子必须印出来，而且"昨天"这个词只在真的是昨天时才用。** 服务停过几天、
        某天她一个字都没写下的时候，最近的一页可能是三天前的；把它说成"昨天"是往她
        眼前塞一句假话，而她会拿它当今天的前一天去接因果。
        """
        if self.day_page is None:
            return "你上一次写下的那一天：（还没有）"
        when = self.day_page.day.strftime("%m-%d")
        if self.day_page.day == living_day_of(self.now) - timedelta(days=1):
            head = f"昨天（{when}）你写下的："
        else:
            head = f"你上一次写下的是 {when}："
        return f"{head}\n{self.day_page.text}"

    def _render_open_ends(self) -> str:
        if not self.open_ends:
            return "心里挂着没了结的：（没有）"
        lines = [f"- {_open_end_line(e, now=self.now)}" for e in self.open_ends]
        return "心里挂着没了结的：\n" + "\n".join(lines)

    def _render_own_recent(self) -> str:
        if not self.own_recent:
            return "你刚做过、说过：（还没有）"
        lines = [
            f"- {dated_clock(h.occurred_at, now=self.now)} {own_line(h)}"
            for h in self.own_recent
        ]
        return "你刚做过、说过：\n" + "\n".join(lines)

    def _render_perceived(self) -> str:
        if not self.perceived.items:
            return "这段时间你感知到的：（没什么动静）"
        lines = [
            f"- {dated_clock(p.occurred_at, now=self.now)} "
            f"{perceived_line(p, me=self.persona_id)}"
            for p in self.perceived.items
        ]
        return "这段时间你感知到的：\n" + "\n".join(lines)


def _open_end_line(end: LooseEnd, *, now: datetime) -> str:
    """她心上一条线头的样子：这件事（可能带该在几点）· 到了没有 · 从哪一缝带过来的。

    **前半段走** :func:`~app.living.loose_ends.format_entry`，所以她读到的形状就是她
    下一缝该照抄回 ``keep_in_mind`` 的形状（整份重写意味着她每一缝都要抄一遍）。抄回
    来解析不出同一件事的话，那条会在她眼皮底下被关掉、再以另一个身份重开。

    **"到点了"在这里当场算，库里没有这个状态。** 有个东西替她把"挂着"改成"到点了"
    就是替她做决定；而且到点之后这条**继续显示**，直到她自己不再列它——时间过了，那个
    会她还是没去开。这跟 :class:`~app.living.records.Upcoming` 到期交付一次就被消费
    掉是两种东西，理由见 :mod:`app.living.loose_ends`。

    ``opened_moment_id`` 而不是只给钟点：跨天之后"12:00 那一缝"分不清是哪一天，而这条
    正是"指得出它是从哪一缝带过来的"这个验收的落点。
    """
    parts = [format_entry(end.what, end.due_at)]
    if end.due_at is not None:
        parts.append("到点了" if end.due_at <= now else "还没到")
    parts.append(f"从 {end.opened_moment_id} 那一缝起挂着")
    return " · ".join(parts)


async def recent_own_happenings(
    *, lane: str, persona_id: str, limit: int = OWN_RECENT_LIMIT
) -> list[Happening]:
    """她自己最近做过 / 说过的若干条，按发生先后升序（最近的在最后）。

    按 ``seq`` 取最近的一段再翻过来：``occurred_at`` 跨 persona 并发时跟落库顺序
    无关，按它排会让同一刻的几条随机换位（见 :func:`app.living.serial.
    append_in_commit_order`）。
    """
    sql = (
        f"SELECT * FROM {_HAPPENING_TABLE} "
        f"WHERE lane = :lane AND actor = :actor "
        f"ORDER BY seq DESC LIMIT :limit"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "actor": persona_id, "limit": limit}
        )
        rows = result.mappings().all()
    items = [
        Happening(**{k: row[k] for k in Happening.model_fields}) for row in rows
    ]
    items.reverse()
    return items


async def all_whereabouts(*, lane: str) -> list[Whereabouts]:
    """本 lane 上每个人此刻在哪、在做什么，各取自己 seq 轴上最新的一条。

    跟 :func:`app.living.whereabouts.who_is_where` 是两个问题：那个只回答"谁在
    哪"（事件写入时拍快照用，位置就够了），这个还要"在做什么"——``look_around``
    要按三档裁出不同的详细程度，同一地点的人在干嘛是看得见的。
    """
    sql = (
        f"SELECT DISTINCT ON (persona_id) * FROM {_WHEREABOUTS_TABLE} "
        f"WHERE lane = :lane ORDER BY persona_id, seq DESC"
    )
    async with get_session() as s:
        result = await s.execute(text(sql), {"lane": lane})
        rows = result.mappings().all()
    return [
        Whereabouts(**{k: row[k] for k in Whereabouts.model_fields}) for row in rows
    ]


async def read_snapshot(
    *, lane: str, persona_id: str, after_seq: int, now: datetime
) -> MomentSnapshot:
    """读她这一缝的全部输入。各层各读各的，谁也不裁谁。

    日记那一页按 ``day < 当前生活日`` 取最新的一页，**不是"最新一页"**：她凌晨写下
    的那页写的是刚过去那一天，而写完的那一刻已经属于新的生活日了。理由和它错了的样子
    写在 :func:`~app.living.day_page.read_day_page_before` 里。
    """
    return MomentSnapshot(
        lane=lane,
        persona_id=persona_id,
        now=now,
        doing=await current_whereabouts(lane=lane, persona_id=persona_id),
        day_page=await read_day_page_before(
            lane=lane, persona_id=persona_id, day=living_day_of(now)
        ),
        open_ends=await list_open_loose_ends(lane=lane, persona_id=persona_id),
        own_recent=await recent_own_happenings(lane=lane, persona_id=persona_id),
        perceived=await read_perceived_by(
            lane=lane,
            persona_id=persona_id,
            after_seq=after_seq,
            limit=PERCEIVED_LIMIT,
        ),
    )
