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
  挂着没了结的事      还开着的 ``LooseEnd``       她自己列多少就是多少
  她刚做过 / 说过     她自己的 ``Happening``      最近 N 条
  这段时间感知到的    ``read_perceived_by``       一条游标 + 每缝的条数上限
  ==================  ==========================  ==============================

**为什么这么长不会失真**：四层没有一层是"对历史的概括"。前两层是当下状态，读一百遍
字字一样；后两层是原文照搬的最近若干条，只是**少**，不是**歪**。失真来自压缩，这里
一处压缩都没有。会被遗忘的只有第三层滚出窗口的那些——而第二层正是她把重要的东西从
滚动窗口里救出来的那只手，救不救是她的决定（见 :mod:`app.living.loose_ends`）。

**第三层为什么必须单独存在**：:func:`~app.living.happening.read_perceived_by` 抑制
回声（``actor == persona_id`` 直接丢），所以她从感知那条路**看不见自己刚说过什么**。
少了这一层，她上一缝答应姐姐的话下一缝就凭空消失，"接得上昨天"永远无从谈起。

**裁剪不在这里重做。** 谁感知得到什么由 T1 的读取路径说了算；这里只负责把已经裁好
的东西摆成她读得懂的样子。只听见动静的那条 ``content`` 本来就是 ``None``，渲染层
再怎么写也漏不出原话。

唯一一处在这里**算**出来的东西是第二层的「到点了 / 还没到」：她给线头挂的时刻当场跟
``now`` 比，库里没有这个状态（:func:`_open_end_line`）。这不是压缩，是把同一个事实
换算成她读得懂的说法——比她自己拿第一行的钟点去减更不容易错，而"这件事算不算了结"
仍然只有她能答。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app.data.session import get_session
from app.infra.cst_time import to_cst_dated, to_cst_full
from app.living.calendar import WORLD_ACTOR
from app.living.happening import Perceived, PerceivedWindow, read_perceived_by
from app.living.loose_ends import LooseEnd, format_entry, list_open_loose_ends
from app.living.records import (
    KIND_SPEECH,
    OUTBOUND_HAPPENING_PREFIX,
    Happening,
    Whereabouts,
)
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
    open_ends: list[LooseEnd]
    own_recent: list[Happening]
    perceived: PerceivedWindow

    def render(self) -> str:
        """摆成她读得懂的样子。四段，每段空的时候如实说空，不留白洞。"""
        return "\n\n".join(
            (
                self._render_now(),
                self._render_hands(),
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

    def _render_open_ends(self) -> str:
        if not self.open_ends:
            return "心里挂着没了结的：（没有）"
        lines = [f"- {_open_end_line(e, now=self.now)}" for e in self.open_ends]
        return "心里挂着没了结的：\n" + "\n".join(lines)

    def _render_own_recent(self) -> str:
        if not self.own_recent:
            return "你刚做过、说过：（还没有）"
        lines = [
            f"- {_clock(h.occurred_at, now=self.now)} {_own_line(h)}"
            for h in self.own_recent
        ]
        return "你刚做过、说过：\n" + "\n".join(lines)

    def _render_perceived(self) -> str:
        if not self.perceived.items:
            return "这段时间你感知到的：（没什么动静）"
        lines = [
            f"- {_clock(p.occurred_at, now=self.now)} "
            f"{_perceived_line(p, me=self.persona_id)}"
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


def _clock(moment: datetime, *, now: datetime) -> str:
    """一条历史记录的时刻：同一个 CST 日历日给 ``HH:MM CST``，跨天补上 ``MM-DD``。

    **不能给裸时分。** ``own_recent`` 按条数取、**不设时间窗**，安静的时候昨晚那几行
    会一直挂在里面（这正是它按条数不按钟点的用意，见 :data:`OWN_RECENT_LIMIT`）；感知
    那段也一样，游标推不动就一直是昨天的动静。而 ``23:41`` 这个形状昨晚和今晚长得一模
    一样，她无从分辨那是几分钟前还是一整天前 —— 线上炸过一次（2026-08-03：中午 13:18
    往群里发「大半夜的发什么疯、赶紧滚去睡觉」）。

    ``now`` 从调用方传进来（``MomentSnapshot.now``），**不在这里现取**：一缝一个 now
    是这套引擎的地基（:mod:`app.living.anchor`），渲染层自己读钟会让同一缝的输入跟它
    的身份对不上。

    走 :func:`app.infra.cst_time.to_cst_dated` 而不是自己比日期：跨天判定要按 CST 日历
    日（不是 UTC、也不是"差了 24 小时"），这条逻辑只该有一处定义。它收的是原始时间串，
    所以这里把已经是 ``datetime`` 的值 ``isoformat()`` 回去 —— aware ISO 正是它认的三
    种格式之一，往返是精确的。
    """
    return to_cst_dated(moment.isoformat(), now=now, seconds=False)


def _message_handle(happening_id: str) -> str | None:
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


def _own_line(h: Happening) -> str:
    """她自己那条记录的样子；发出去的消息末尾带上它的编号。

    带编号是**给她一个消息级句柄**。没有它的时候她只能拿原话去指要撤哪一条，于是
    后端得在逻辑层按内容猜——同一句话说过两遍就分不出是哪一次。真人撤消息是看着那条
    点的，句柄一直都在他手上；她的句柄就印在这里。

    整串照印，不截断：截断要配一套前缀唯一性校验，而那个分支在真实数据量下永远不会
    触发。她是模型，照抄一串字符没有负担。
    """
    handle = _message_handle(h.happening_id)
    tail = f"［{handle}］" if handle is not None else ""
    if h.kind != KIND_SPEECH:
        return f"你 {h.content}{tail}"
    if h.audience:
        return f"你对 {'、'.join(h.audience)} 说：「{h.content}」{tail}"
    return f"你说：「{h.content}」{tail}"


def _perceived_line(p: Perceived, *, me: str) -> str:
    """一条感知记录的样子。``content is None`` 时**没有任何口子**能漏出原话。"""
    if p.content is None:
        return f"{p.place} 那边有动静"
    if p.actor == WORLD_ACTOR:
        # 世界自己发生的事（天黑、快递到）没有"谁"，加个主语就是在编人。
        return p.content
    if p.kind != KIND_SPEECH:
        return f"{p.actor} {p.content}" + ("（是冲着你来的）" if p.directed else "")
    if p.directed:
        # 一句话可以同时说给两个人；只说"对你说"会让她看不见姐姐也在场。
        others = [a for a in p.audience if a != me]
        also = f"和 {'、'.join(others)} " if others else ""
        return f"{p.actor} 对你{also}说：「{p.content}」"
    if p.audience:
        return f"{p.actor} 对 {'、'.join(p.audience)} 说：「{p.content}」"
    return f"{p.actor} 说：「{p.content}」"


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
    """读她这一缝的全部输入。四层各读各的，谁也不裁谁。"""
    return MomentSnapshot(
        lane=lane,
        persona_id=persona_id,
        now=now,
        doing=await current_whereabouts(lane=lane, persona_id=persona_id),
        open_ends=await list_open_loose_ends(lane=lane, persona_id=persona_id),
        own_recent=await recent_own_happenings(lane=lane, persona_id=persona_id),
        perceived=await read_perceived_by(
            lane=lane,
            persona_id=persona_id,
            after_seq=after_seq,
            limit=PERCEIVED_LIMIT,
        ),
    )
