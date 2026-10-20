"""跨天沉淀 —— 刚过去那一天收拢成一页，她第二天读得到。

**她跨不过一天。** 快照那几层（:mod:`app.living.life.now`）全是"当下"：手上的事、
心里挂着没了结的、她自己最近十二条（只在上下文接不住她最近说过的话时给）；传到她这里
的消息也只摆一次。滚出窗口的东西没有任何一层接得住，所以今天问她昨天干了什么，她答不
上来。

**做法不是压缩，是让她自己另写一页。** 机器折叠原文那条路 snapshot 的 docstring 已
经否掉了（``SessionTranscript`` 每 100 条叫一次模型概括：折叠完原文就没了，压错了没
人知道）。这里一条原始记录都不动，只是每天凌晨把刚过去那一天原样摆给她，让她**另
写**一页 —— 跟 :mod:`app.living.loose_ends` 同一个性质，这个包里第二处「她自己写下
的东西」。

五件必须真的成立：

  * **生活日不是日历日。** 凌晨三点还醒着的时候那是昨天的延续，边界在 CST 04:00
    （:data:`DAY_STARTS_AT`）。按日历日切会把一段连续的清醒劈成两天，而她自己不会
    那样记。
  * **"这天复盘过了"的权威是页本身存在**（:class:`LivingDayPage` 那一行在不在），
    绝不另设标记列。理由写在 :class:`LivingDayPage` 的 docstring 里。
  * **``run`` 返回不等于成功。** 这一轮**不给她任何工具**，她的回复正文**就是**那一
    页，于是"调了工具却没写成"这整类失败根本不存在。正文 strip 后为空 = 这一轮没
    成：不落库、不占位，下一拍还会来（:func:`write_due_page`）。
  * **摆给她的是她自己的经历**（:func:`day_material`）：她做过的事、说过的话，加上那
    一天传到她这里的消息。别人做了什么，只有传到她这里的那一部分才算她知道；别的姐妹的
    记录是她们自己的，这里一条都不读。
  * **注入侧读的是 ``day < 当前生活日`` 的最新一页**，不是"最新一页"
    （:func:`read_day_page_before`）。她凌晨写下的是刚过去那天的页，不卡这一条的话
    第二天的页会在写下的当天就被当成"你的昨天"喂回去，她会把今天当昨天过。

prompt 在 Langfuse（:data:`DAY_PAGE_PROMPT_ID`），变量只有 persona 那两个；那一天的
材料和上一页走 USER 消息 —— 它们每天都变，而 prompt 变量改名会**静默**渲染成字面量
（同 :mod:`app.living.life.moment`）。
"""

from __future__ import annotations

import heapq
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Annotated

from pydantic import field_validator

from app.agent.contracts import AgentSpec, Segment
from app.agent.core import Agent
from app.agent.neutral import Message, Role
from app.agent.prompts import PromptSet
from app.agent.round import Round, open_call
from app.agent.trace import collect_usage
from app.data.persist import fetch_all, fetch_one, from_row, insert_idempotent
from app.data.schema import Data, Key, table_name
from app.domain.thinking_cost import record_round_cost
from app.infra.cst_time import CST, dated_clock
from app.living.common import require_aware
from app.living.services import HerRecords, Persona, Received

logger = logging.getLogger(__name__)

# 生活日的边界：CST 凌晨四点。一个生活日是 ``[04:00, 次日 04:00)``。
DAY_STARTS_AT = time(4, 0)

# 回望刚过去那一天的窗口，左闭右开。左边界对齐生活日的边界：那一天到 04:00 才真的
# 结束，早于它去写就是在写一天的一半。右边界给两个钟头，是留给"这一拍没写成"的余地
# ——模型瞬时失败、她一个字都没写，窗口里后面每一拍都还会再来一次（页不存在就还会
# 写）。窗口过了就不再回望：中午再补一页"昨天"，她读到的时候那已经是前天了。
DAY_PAGE_FROM = time(4, 0)
DAY_PAGE_UNTIL = time(6, 0)

# Langfuse prompt id。
DAY_PAGE_PROMPT_ID = "living_day_page"

# offline-model：一天三次（三个人各一次），是低频的离线调用，不该占 life 那条高频线的
# 档位。
#
# recursion_limit 1：这一轮**没有工具**，一次生成就完。这不是省钱，是把一整类失败
# 从存在里去掉——有工具的话"她调了工具但正文写空了"是一种既没报错、也没有页的状态，
# 而这里她的回复正文就是那一页，写没写成只有一个判据。
#
# 一个人的日记在 langfuse 里读成一条流（session），逐天翻起来才不用大海捞针。
DAY_PAGE_AGENT = AgentSpec(
    segments=(DAY_PAGE_PROMPT_ID,),
    model="offline-model",
    trace_name="living-day-page",
    recursion_limit=1,
    session="living-day-page:{lane}:{participant}",
)
DAY_PAGE_SEGMENT = Segment(
    DAY_PAGE_PROMPT_ID, DAY_PAGE_PROMPT_ID, variables=("persona_name", "persona_core")
)


class LivingDayPage(Data):
    """她给某一个生活日写下的那一页。

    **为什么不叫 ``DayPage``。** 表名是从类名派生的
    （:func:`app.data.schema.table_name` 对类名做 snake_case、前面加
    ``data_``），所以叫 ``DayPage`` 就会落到
    ``data_day_page`` 这张表上。那张表是已经删掉的旧引擎（睡前回顾）的，列是
    ``lane / persona_id / date / narrative / written_at / version / dedup_hash /
    created_at``，跟这里声明的字段对不上；而且它不是一张废表——prod 上有 509 行真实
    历史（2026-06-11 起），旧的写入方今天仍然在往里写。migrator 只增不减，看到
    ``date`` / ``narrative`` / ``version`` 三列没有任何字段认领就抛
    ``MigrationError``，整批迁移回滚、Pod 起不来。**这一条在测试里看不见**：测试是
    在空库里建新表，只有部署到已经有那张旧表的库上才炸（coe-living 上实际发生过）。
    所以这个名字是被一张还活着的旧表挡出来的，不是随手起的——别当成冗余前缀"清理"回
    ``DayPage``。这条由
    ``tests/living/test_day_page.py::test_the_page_table_is_not_the_old_engines_day_page_table``
    钉住。

    自然键 ``(lane, persona_id, day)``，纯 append 无版本链——一天写完就是写完了，没有
    "改一页旧日记"的语义。

    **这一行存在本身就是"这天复盘过了"**，不另设 ``written`` / ``done`` 之类的标记
    列。标记和内容是两个事实，中间崩一次就永久对不上：标了但没内容 = 这一天她永远
    丢了；有内容但没标 = 每一拍重写一遍、每次都烧一份钱。判据只有一个的时候，这两种
    状态都不存在。

    ``day`` 是 :class:`datetime.date`（DATE 列），不是时刻也不是文本。它答的是"哪个
    生活日"，而生活日的边界是钟点（:data:`DAY_STARTS_AT`）——存成一个时刻等于要求每
    个读取方自己再换算一次边界，存成文本则连"比这天早的最新一页"这个查询都撑不起来。

    ``happenings`` 是摆给她的材料有几行。**不是日志，是事后判据**：一页写得空的时候
    要能分清"那天真的没什么事"和"她这一轮敷衍了"，光看正文分不出来。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    day: Annotated[date, Key]   # 这一页写的是哪个生活日
    text: str                   # 她写下的那一页，原文
    written_at: datetime        # 她写下它的时刻
    happenings: int             # 摆给她的材料有几行

    class Meta:
        # 读侧唯一形状：某个人比某一天更早的最新一页（注入那条路，每一轮都走）。
        # 键列上没有自动索引（migrator 只给 dedup_hash 和 Version 类建），不声明的话
        # 这条查询随着日记一天天变长而变慢，而症状只是"她那一轮有点久"。
        indexes = (("lane", "persona_id", "day"),)

    @field_validator("written_at")
    @classmethod
    def _aware_written_at(cls, v: datetime) -> datetime:
        # 同 living 另外几张表：naive 落进 TIMESTAMPTZ 会被按服务器时区解释、静默偏
        # 几小时（见 app.living.her_records.records）。
        return require_aware("written_at", v)


_TABLE = table_name(LivingDayPage)


def living_day_of(moment: datetime) -> date:
    """这一刻属于哪个生活日。

    生活日从 CST :data:`DAY_STARTS_AT` 起算，所以把时刻往回推那么多再取日历日：凌晨
    三点推回去落在前一天，四点整推回去正好落在当天零点。**不是按日历日切**——凌晨还
    醒着的那一段是昨天的延续，切开的话她那一天会从中间断成两页。
    """
    local = moment.astimezone(CST) - timedelta(
        hours=DAY_STARTS_AT.hour, minutes=DAY_STARTS_AT.minute
    )
    return local.date()


def living_day_bounds(day: date) -> tuple[datetime, datetime]:
    """这个生活日的 ``[起, 止)``：CST ``04:00`` 到次日 ``04:00``。

    CST 是固定偏移，所以"加一天"就是精确的 24 小时，不用担心夏令时那类跳变。
    """
    start = datetime.combine(day, DAY_STARTS_AT, tzinfo=CST)
    return start, start + timedelta(days=1)


async def day_material(
    *, lane: str, persona_id: str, day: date, her_records: HerRecords, received: Received
) -> list[str]:
    """这一天她自己的经历，一件一行：她做过的事、说过的话，和传到她这里的消息。

    **原文照搬，中间没有第二次概括** —— 整个模块存在的理由就是"不折叠原文"，在摆材
    料这一步先压一遍等于把否掉的方案从后门放回来。

    两样各有各的样子，跟她每一轮读到的一样：她自己的走
    :meth:`~app.living.services.HerRecords.own_line`（手机上发的、撤回的也在，她自己回看得到），
    收到的走 :meth:`~app.living.services.Received.line`（``received`` 服务）。

    **按事情发生的先后合成一条**：她的经历按提交序（同一轮里的几件钟点相同，谁先谁后只有
    提交序说得清），收到的按消息自带的时间（world 的告知和姐妹的话走两条路，到达先后不代表
    发生先后）。两列各自的先后不动，按时刻交错合起来；时刻相同的，收到的排在前面——先传到
    她这里，她才接着做。

    每行的时刻用**这个生活日的起点**当 ``now`` 判跨不跨天，不是用"现在"：一个生活日
    跨两个日历日，凌晨那几行只给 ``HH:MM`` 的话读起来像"这天很早"，而拿真正的现在去
    判会让整整一天的行全都带上日期、把"这几行是跨过午夜的"这个信号稀释掉。
    """
    since, until = living_day_bounds(day)
    heard = [
        (m.message_time, received.line(m))
        for m in await received.between(
            lane=lane, persona_id=persona_id, since=since, until=until
        )
    ]
    did = [
        (h.occurred_at, her_records.own_line(h))
        for h in await her_records.happenings_between(
            lane=lane, persona_id=persona_id, since=since, until=until
        )
    ]
    return [
        f"{dated_clock(at, now=since)} {line}"
        for at, line in heapq.merge(heard, did, key=lambda item: item[0])
    ]


async def read_day_page(
    *, lane: str, persona_id: str, day: date
) -> LivingDayPage | None:
    """她给这个生活日写下的那一页；没写过返回 ``None``。

    这就是"这天复盘过了没有"的**全部**判据（见 :class:`LivingDayPage`）。
    """
    sql = (
        f"SELECT * FROM {_TABLE} WHERE lane = :lane "
        f"AND persona_id = :persona_id AND day = :day LIMIT 1"
    )
    row = await fetch_one(sql, lane=lane, persona_id=persona_id, day=day)
    return None if row is None else from_row(LivingDayPage, row)


async def read_day_page_before(
    *, lane: str, persona_id: str, day: date
) -> LivingDayPage | None:
    """**严格早于** ``day`` 的那些页里最新的一页；一页都没有返回 ``None``。

    "严格早于"是这条注入的全部要害，不是随手加的过滤。她凌晨写下的是**刚过去那天**
    的页，而写完的那一刻已经属于新的生活日了。按"最新一页"注入的话，第二天凌晨她写
    下 07-26 那页之后，07-27 一整天读到的"昨天"仍然是 07-25 —— 差一天，而且没有任何
    报错，只有她把今天当昨天过。

    这个函数同时是写页那一轮拿上一页的入口（:func:`write_due_page`）：链式，让她写
    这一页时看得见上一页。不给的话每一页都是孤立的一天，跨天这条链断在第二页。
    """
    sql = (
        f"SELECT * FROM {_TABLE} WHERE lane = :lane "
        f"AND persona_id = :persona_id AND day < :day "
        f"ORDER BY day DESC LIMIT 1"
    )
    row = await fetch_one(sql, lane=lane, persona_id=persona_id, day=day)
    return None if row is None else from_row(LivingDayPage, row)


async def read_day_pages_between(
    *, lane: str, persona_id: str, since: date, until: date
) -> list[LivingDayPage]:
    """``[since, until)`` 这段日子里她写下的每一页，**按 ``day`` 升序**；一页都没有
    返回空列表。

    每周回看（:mod:`app.living.persona_review`，经 ``day_pages`` 服务）读的就是这个：一整周的日记原文按她
    过日子的顺序摆到她眼前。

    **右开**跟 :func:`read_day_page_before` 的"严格早于"是同一条理由：调用方给的
    ``until`` 是下一周的第一天，闭上就会多带一天进来，而那一天她当时还没过完。

    升序不是可选项：读一周的日记是读一段时间的推移，乱序之后每一页仍然读得通、
    合起来仍然像一周，只是她感觉到的先后是错的 —— 没有任何东西会因此报错。

    索引走 ``LivingDayPage.Meta.indexes`` 那条 ``(lane, persona_id, day)``，跟"比某天早的
    最新一页"共用同一条，不另加。
    """
    sql = (
        f"SELECT * FROM {_TABLE} WHERE lane = :lane "
        f"AND persona_id = :persona_id AND day >= :since AND day < :until "
        f"ORDER BY day ASC"
    )
    rows = await fetch_all(sql, lane=lane, persona_id=persona_id, since=since, until=until)
    return [from_row(LivingDayPage, row) for row in rows]


def _day_page_prompt(*, day: date, lines: list[str], previous: LivingDayPage | None) -> str:
    """摆到她眼前的那一段：这是哪一天、这一天发生过什么、上一页写了什么。

    上一页给**原文**而不是"上一页写过了"这类提示：她要看得见自己昨天怎么写的，才接
    得住那条线（昨天挂在心上的事今天怎么样了）。没有上一页时如实说空，不留白洞——
    留白的话她会以为材料被截断了，转去补一段自己编的前情。
    """
    before = (
        f"你上一次写下的那一天（{previous.day.strftime('%m-%d')}）：\n{previous.text}"
        if previous is not None
        else "你还没有写下过任何一天。"
    )
    return (
        f"刚过去的这一天是 {day.strftime('%Y-%m-%d')}"
        f"（从这天凌晨四点到第二天凌晨四点）。\n\n"
        f"这一天你这边是这样的：\n" + "\n".join(lines) + "\n\n"
        f"{before}\n\n"
        f"把这一天写成你自己的一页。"
    )


@dataclass(frozen=True)
class DuePage:
    """判出来要写的那一页：哪一天、她这一轮的『现在』、摆给她的那一天。"""

    # 为什么写这一页，记进日志。
    reason: str
    lane: str
    persona_id: str
    # 要写的是哪个生活日。
    day: date
    # 她这一轮的『现在』：轮到她时读的钟，这一页记成这一刻写下的。
    now: datetime
    # 那一天的材料，一件一行（:func:`day_material`）。
    lines: tuple[str, ...]


async def day_page_due(
    *,
    lane: str,
    persona_id: str,
    at: datetime,
    clock: Callable[[], datetime],
    her_records: HerRecords,
    received: Received,
) -> DuePage | None:
    """日记的钟在 ``at`` 响的那一拍，轮到她的日记队列时：要写哪一页；不该写返回 ``None``。

    **先读钟**：轮到她的这一刻就是她这一轮的『现在』，写哪一天、这一页什么时候写下的都按它。前一次
    写页挂住时（最长占着她的日记队列 15 分钟），排在后面的那一拍要过一阵才轮到，这一页是那时写下
    的，不是那一拍敲响时。

    **窗口按那一拍响的时刻判**（``at``，换到北京时间）：窗口只管这一拍要不要去试。排在后面、
    轮到时已经过了窗口的那一拍照样写，不然那一天就再也没有页了；窗口之前响的那一拍轮到时进了
    窗口，也不写。进程停过、06:00 之后才起来，那一天的窗口就过了，这一天不写。

    之后两道判断都在模型前面，能不调模型就不调：

      1. 要写的是**刚结束的那个生活日**（``living_day_of(now) - 1``），不是今天。**这天已经有页**
         就返回：页存在就是复盘过了，重复的拍不该再烧一次模型。
      2. **那天什么都没发生**（材料一行都没有）就返回：那是服务根本没跑的一天，不该有那一页，
         更不该为一片空白叫一次模型。

    判和写在她日记队列上的同一次轮次里（:mod:`app.living.day_page`）：两拍打到同一个人时，各自读到
    "这天还没写"就会双双烧一次模型，最后还有一条被 ``insert_idempotent`` 丢掉。队列上每一拍都
    排队，轮到时再判一遍。
    """
    now = clock()
    if not (DAY_PAGE_FROM <= at.astimezone(CST).time() < DAY_PAGE_UNTIL):
        return None
    day = living_day_of(now) - timedelta(days=1)
    if await read_day_page(lane=lane, persona_id=persona_id, day=day) is not None:
        return None
    lines = await day_material(
        lane=lane, persona_id=persona_id, day=day, her_records=her_records, received=received
    )
    if not lines:
        logger.info(
            "living day page lane=%s persona=%s %s 这一天一件事都没有，不写",
            lane,
            persona_id,
            day.isoformat(),
        )
        return None
    return DuePage(
        reason=f"写 {day.isoformat()} 那一页",
        lane=lane,
        persona_id=persona_id,
        day=day,
        now=now,
        lines=tuple(lines),
    )


async def write_due_page(due: DuePage, *, persona: Persona) -> LivingDayPage | None:
    """让她把判出来的那一天（:class:`DuePage`）写成一页；没写成返回 ``None``。「她是谁」那两个
    prompt 变量从 ``persona`` 服务取（:meth:`app.living.services.Persona.prompt_vars`），和她的一轮
    是同一份。

    模型跑完之后还有一道：**正文 strip 为空 = 这一轮没成**。不落库、不占位——下一拍还在窗口里的
    话她会再写一次。拿空页把这天记成"写过了"是最坏的一种，因为那一天从此永远丢了，而且看起来
    一切正常。

    模型的临时失败不整轮重来（:data:`DAY_PAGE_AGENT` 的 ``max_retries`` 是 1）：重放白花一次钱；
    这一轮本来就低频，五分钟后那一拍再来就行。模型失败往外抛，由她日记队列上的钟记一条
    warning（一个人的失败不拖累另两个人）。
    """
    lane, persona_id, day = due.lane, due.persona_id, due.day
    previous = await read_day_page_before(lane=lane, persona_id=persona_id, day=day)
    prompt_vars = await persona.prompt_vars(lane=lane, persona_id=persona_id)
    call = open_call(DAY_PAGE_PROMPT_ID, DAY_PAGE_AGENT, lane=lane, participant=persona_id)
    prompts = await PromptSet.resolve(
        {DAY_PAGE_PROMPT_ID: DAY_PAGE_AGENT},
        {DAY_PAGE_PROMPT_ID: DAY_PAGE_SEGMENT},
        None,
        participant=persona_id,
    )
    # 用量落 durable PG，理由同 moment（见 app.agent.trace）：langfuse
    # 会系统性丢 trace，"这一天花了多少"只能从 PG 数。
    with collect_usage() as usage:
        reply = await Agent(DAY_PAGE_AGENT).run(
            [
                Message(
                    role=Role.USER,
                    content=_day_page_prompt(
                        day=day, lines=list(due.lines), previous=previous
                    ),
                )
            ],
            call=call,
            prompts=prompts,
            values=prompt_vars,
        )

    await record_round_cost(
        lane=lane,
        actor=persona_id,
        round_id=f"day-page:{day.isoformat()}",
        usage=usage,
        observed_at=due.now.isoformat(),
    )

    written = reply.text().strip()
    if not written:
        logger.warning(
            "living day page lane=%s persona=%s %s 这一轮一个字都没写，不落库",
            lane,
            persona_id,
            day.isoformat(),
        )
        return None

    page = LivingDayPage(
        lane=lane,
        persona_id=persona_id,
        day=day,
        text=written,
        written_at=due.now,
        happenings=len(due.lines),
    )
    await insert_idempotent(page)
    logger.info(
        "living day page lane=%s persona=%s 写下了 %s（材料 %d 行）：%s",
        lane,
        persona_id,
        day.isoformat(),
        page.happenings,
        page.text,
    )
    return page


# ---------------------------------------------------------------------------
# 插件对象：她一轮里"上一次写下的那一天"那一段、day_pages 服务、写一页要用的服务
# ---------------------------------------------------------------------------


class Diary:
    """day_page 插件对象：她一轮里"上一次写下的那一天"那一段，和 ``day_pages`` 服务
    （:class:`app.living.services.DayPages`，每周回看经它读一周的日记）。写一页时她是谁问 ``persona``
    服务，那一天她做过、说过的问 ``her_records`` 服务，传到她这里的问 ``received`` 服务。不带状态。"""

    def __init__(self, *, persona: Persona, her_records: HerRecords, received: Received) -> None:
        self.persona = persona
        self.her_records = her_records
        self.received = received

    async def between(
        self, *, lane: str, persona_id: str, since: date, until: date
    ) -> list[LivingDayPage]:
        """``[since, until)`` 这段日子里她写下的每一页，按日子升序（:func:`read_day_pages_between`）。"""
        return await read_day_pages_between(
            lane=lane, persona_id=persona_id, since=since, until=until
        )

    async def last_written_section(self, round: Round) -> str:
        """状态：她上一次写下的那一天。它是背景，不是此刻正在发生的事。

        读的是**严格早于**当前生活日的最新一页（:func:`read_day_page_before`，理由写在那儿）。

        **日子必须印出来，而且"昨天"这个词只在真的是昨天时才用。** 服务停过几天、某天她一个字都没
        写下的时候，最近的一页可能是三天前的；把它说成"昨天"是往她眼前塞一句假话，而她会拿它当今天的
        前一天去接因果。空的时候如实说空，不留白洞；**不带时刻**：界桩自己头上就印着这次清理的时刻。
        """
        today = living_day_of(round.now)
        page = await read_day_page_before(
            lane=round.lane, persona_id=round.participant, day=today
        )
        if page is None:
            return "你上一次写下的那一天：（还没有）"
        when = page.day.strftime("%m-%d")
        if page.day == today - timedelta(days=1):
            head = f"昨天（{when}）你写下的："
        else:
            head = f"你上一次写下的是 {when}："
        return f"{head}\n{page.text}"
