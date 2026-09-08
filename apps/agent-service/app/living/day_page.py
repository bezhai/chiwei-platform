"""跨天沉淀 —— 刚过去那一天收拢成一页，她第二天读得到。

**她跨不过一天。** 快照那四层（:mod:`app.living.snapshot`）全是"当下"：手上的事、
心里挂着没了结的、她自己最近十二条、游标之后那一段。滚出窗口的东西没有任何一层接得
住，所以今天问她昨天干了什么，她答不上来。

**做法不是压缩，是让她自己另写一页。** 机器折叠原文那条路 snapshot 的 docstring 已
经否掉了（``SessionTranscript`` 每 100 条叫一次模型概括：折叠完原文就没了，压错了没
人知道）。这里一条原始记录都不动，只是每天凌晨把刚过去那一天原样摆给她，让她**另
写**一页 —— 跟 :mod:`app.living.loose_ends` 同一个性质，这个包里第二处「她自己写下
的东西」。

六件必须真的成立：

  * **生活日不是日历日。** 凌晨三点还醒着的时候那是昨天的延续，边界在 CST 04:00
    （:data:`DAY_STARTS_AT`）。按日历日切会把一段连续的清醒劈成两天，而她自己不会
    那样记。
  * **"这天复盘过了"的权威是页本身存在**（:class:`LivingDayPage` 那一行在不在），
    绝不另设标记列。理由写在 :class:`LivingDayPage` 的 docstring 里。
  * **``run`` 返回不等于成功。** 这一轮**不给她任何工具**，她的回复正文**就是**那一
    页，于是"调了工具却没写成"这整类失败根本不存在。正文 strip 后为空 = 这一轮没
    成：不落库、不占位，下一拍还会来（:func:`write_day_page`）。
  * **摆给她的是她的那一天**，不是全世界的那一天：够不着的事她当时就不知道，日记里
    不该冒出来（:func:`day_material` 逐条过 :func:`app.living.happening.happening_line`）。
  * **她自己做的事必须在材料里。** 感知那条路抑制回声，照抄过来的话她的一天里只剩
    别人做的事，自己那些一件不剩。
  * **注入侧读的是 ``day < 当前生活日`` 的最新一页**，不是"最新一页"
    （:func:`read_day_page_before`）。她凌晨写下的是刚过去那天的页，不卡这一条的话
    第二天的页会在写下的当天就被当成"你的昨天"喂回去，她会把今天当昨天过。

prompt 在 Langfuse（:data:`DAY_PAGE_PROMPT_ID`），变量只有 persona 那两个；那一天的
材料和上一页走 USER 消息 —— 它们每天都变，而 prompt 变量改名会**静默**渲染成字面量
（同 :mod:`app.living.moment`）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Annotated

from pydantic import field_validator
from sqlalchemy import text

from app.agent.context import AgentContext
from app.agent.core import AgentConfig
from app.agent.neutral import Message, Role
from app.agent.trace import collect_usage
from app.capabilities.agent import AgentRunner
from app.data.session import get_session
from app.domain.thinking_cost import record_round_cost
from app.infra.cst_time import CST, now_cst
from app.living.clock import living_lane
from app.living.happening import happening_line, read_happenings_between
from app.living.persona import LIVING_PERSONAS, persona_prompt_vars
from app.living.records import _require_aware
from app.living.serial import hold
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.node import node
from app.runtime.persist import insert_idempotent

logger = logging.getLogger(__name__)

# 生活日的边界：CST 凌晨四点。一个生活日是 ``[04:00, 次日 04:00)``。
DAY_STARTS_AT = time(4, 0)

# 回望刚过去那一天的窗口，左闭右开。左边界对齐生活日的边界：那一天到 04:00 才真的
# 结束，早于它去写就是在写一天的一半。右边界给两个钟头，是留给"这一拍没写成"的余地
# ——模型瞬时失败、她一个字都没写，窗口里后面每一拍都还会再来一次（页不存在就还会
# 写）。窗口过了就不再回望：中午再补一页"昨天"，她读到的时候那已经是前天了。
DAY_PAGE_FROM = time(4, 0)
DAY_PAGE_UNTIL = time(6, 0)

# 钟拍得比窗口密得多，因为"该不该写"判在节点里（在不在窗口里、这天有没有页），绝大
# 多数拍在打模型之前就返回了。五分钟一拍 = 窗口里有 24 次机会。
DAY_PAGE_TICK_SECONDS = 300

# Langfuse prompt id。
DAY_PAGE_PROMPT_ID = "living_day_page"

# offline-model：一天三次（三个人各一次），跟 world 的轮次同一个量级，不该占 life
# 那条高频线的档位。
#
# recursion_limit 1：这一轮**没有工具**，一次生成就完。这不是省钱，是把一整类失败
# 从存在里去掉——有工具的话"她调了工具但正文写空了"是一种既没报错、也没有页的状态，
# 而这里她的回复正文就是那一页，写没写成只有一个判据。
_DAY_PAGE_CFG = AgentConfig(
    DAY_PAGE_PROMPT_ID,
    "offline-model",
    "living-day-page",
    recursion_limit=1,
)


class LivingDayPage(Data):
    """她给某一个生活日写下的那一页。

    **为什么不叫 ``DayPage``。** 表名是从类名派生的
    （:func:`app.runtime.migrator._table_name` 对类名做 snake_case、前面加
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
        # 读侧唯一形状：某个人比某一天更早的最新一页（注入那条路，每一缝都走）。
        # 键列上没有自动索引（migrator 只给 dedup_hash 和 Version 类建），不声明的话
        # 这条查询随着日记一天天变长而变慢，而症状只是"她那一缝有点久"。
        indexes = (("lane", "persona_id", "day"),)

    @field_validator("written_at")
    @classmethod
    def _aware_written_at(cls, v: datetime) -> datetime:
        # 同 living 另外几张表：naive 落进 TIMESTAMPTZ 会被按服务器时区解释、静默偏
        # 几小时（见 app.living.records）。
        return _require_aware("written_at", v)


_TABLE = _table_name(LivingDayPage)


def day_page_lock_key(lane: str, persona_id: str) -> str:
    """这个人在这个 lane 上写日记的排他占用 key（每人一条轴，互不阻塞）。"""
    return f"living:day-page:{lane}:{persona_id}"


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


async def day_material(*, lane: str, persona_id: str, day: date) -> list[str]:
    """这一天在她眼里发生过的每一件事，一件一行；她感知不到的不出现。

    **原文照搬，中间没有第二次概括** —— 整个模块存在的理由就是"不折叠原文"，在摆材
    料这一步先压一遍等于把否掉的方案从后门放回来。

    逐条过 :func:`app.living.happening.happening_line`：她自己做的走
    :func:`~app.living.happening.own_line`，别人的先过
    :func:`~app.living.happening.perceive` 按当时在不在场裁。直接用
    :func:`~app.living.happening.read_perceived_by` 是错的——那条路抑制回声（丢掉
    ``actor == persona_id``），对一缝是对的，对"回看这一整天"是致命的：她的一天里会
    只剩别人做的事。

    每行的时刻用**这个生活日的起点**当 ``now`` 判跨不跨天，不是用"现在"：一个生活日
    跨两个日历日，凌晨那几行只给 ``HH:MM`` 的话读起来像"这天很早"，而拿真正的现在去
    判会让整整一天的行全都带上日期、把"这几行是跨过午夜的"这个信号稀释掉。
    """
    since, until = living_day_bounds(day)
    rows = await read_happenings_between(lane=lane, since=since, until=until)
    lines = [happening_line(h, me=persona_id, now=since) for h in rows]
    return [line for line in lines if line is not None]


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
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id, "day": day}
        )
        row = result.mappings().first()
    if row is None:
        return None
    return LivingDayPage(**{k: row[k] for k in LivingDayPage.model_fields})


async def read_day_page_before(
    *, lane: str, persona_id: str, day: date
) -> LivingDayPage | None:
    """**严格早于** ``day`` 的那些页里最新的一页；一页都没有返回 ``None``。

    "严格早于"是这条注入的全部要害，不是随手加的过滤。她凌晨写下的是**刚过去那天**
    的页，而写完的那一刻已经属于新的生活日了。按"最新一页"注入的话，第二天凌晨她写
    下 07-26 那页之后，07-27 一整天读到的"昨天"仍然是 07-25 —— 差一天，而且没有任何
    报错，只有她把今天当昨天过。

    这个函数同时是写页那一轮拿上一页的入口（:func:`write_day_page`）：链式，让她写
    这一页时看得见上一页。不给的话每一页都是孤立的一天，跨天这条链断在第二页。
    """
    sql = (
        f"SELECT * FROM {_TABLE} WHERE lane = :lane "
        f"AND persona_id = :persona_id AND day < :day "
        f"ORDER BY day DESC LIMIT 1"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id, "day": day}
        )
        row = result.mappings().first()
    if row is None:
        return None
    return LivingDayPage(**{k: row[k] for k in LivingDayPage.model_fields})


async def read_day_pages_between(
    *, lane: str, persona_id: str, since: date, until: date
) -> list[LivingDayPage]:
    """``[since, until)`` 这段日子里她写下的每一页，**按 ``day`` 升序**；一页都没有
    返回空列表。

    每周回看（:mod:`app.living.persona_review`）读的就是这个：一整周的日记原文按她
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
    async with get_session() as s:
        result = await s.execute(
            text(sql),
            {
                "lane": lane,
                "persona_id": persona_id,
                "since": since,
                "until": until,
            },
        )
        rows = result.mappings().all()
    return [
        LivingDayPage(**{k: row[k] for k in LivingDayPage.model_fields})
        for row in rows
    ]


def build_day_page_runner() -> AgentRunner:
    """写这一页的 agent。模块级函数，测试替身从这里换掉，不碰真模型。

    **不带任何工具**（见 :data:`_DAY_PAGE_CFG`）：她的回复正文就是那一页。
    """
    return AgentRunner(_DAY_PAGE_CFG)


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


async def write_day_page(
    *, lane: str, persona_id: str, now: datetime
) -> LivingDayPage | None:
    """让她把刚过去那个生活日写成一页；这一拍不该写 / 没写成就返回 ``None``。

    **"这天写过没有"到落库为止在排他占用里**（每人一条轴），理由同 world 的轮次：两
    条拍打到同一个人时，各自读到"这天还没写"就会双双烧一次模型，最后还有一条被
    ``insert_idempotent`` 丢掉。窗口那一道判在占用**外面**：它只看传进来的 ``now``，
    不读任何共享状态，等锁没有意义——一天里 288 拍中的绝大多数在这里就返回了。

    顺序是"能不调模型就不调"——四道判断全在模型前面：

      1. **不在窗口里**（:data:`DAY_PAGE_FROM` ～ :data:`DAY_PAGE_UNTIL`）就返回。
         半夜两点她还在过昨天，中午了这件事早该做完，都不是回望的时候。
      2. 要写的是**刚结束的那个生活日**（``living_day_of(now) - 1``），不是今天。
      3. **这天已经有页**就返回。页存在就是复盘过了，重复的拍不该再烧一次模型。
      4. **那天什么都没发生**（材料一行都没有）就返回：那是服务根本没跑的一天，不该
         有那一页，更不该为一片空白叫一次模型。

    模型跑完之后还有一道：**正文 strip 为空 = 这一轮没成**。不落库、不占位——下一拍
    还在窗口里的话她会再写一次。拿空页把这天记成"写过了"是最坏的一种，因为那一天从
    此永远丢了，而且看起来一切正常。

    ``max_retries=1``：core 的 ``run`` 把整轮包在 ``@retry`` 里，一次模型瞬时失败会
    整轮重放、白花一次钱；这一轮本来就低频，五分钟后那一拍再来就行。
    """
    local = now.astimezone(CST)
    if not (DAY_PAGE_FROM <= local.time() < DAY_PAGE_UNTIL):
        return None

    day = living_day_of(now) - timedelta(days=1)
    async with hold(day_page_lock_key(lane, persona_id)):
        if await read_day_page(lane=lane, persona_id=persona_id, day=day) is not None:
            return None

        lines = await day_material(lane=lane, persona_id=persona_id, day=day)
        if not lines:
            logger.info(
                "living day page lane=%s persona=%s %s 这一天一件事都没有，不写",
                lane,
                persona_id,
                day.isoformat(),
            )
            return None

        previous = await read_day_page_before(
            lane=lane, persona_id=persona_id, day=day
        )
        prompt_vars = await persona_prompt_vars(lane=lane, persona_id=persona_id)
        # 一个人的日记在 langfuse 里读成一条流，逐天翻起来才不用大海捞针。没有工具，
        # 所以不需要任何 ambient feature。
        context = AgentContext(
            persona_id=persona_id,
            session_id=f"living-day-page:{lane}:{persona_id}",
        )
        # 用量落 durable PG，理由同一缝和 world 轮次（见 app.agent.trace）：langfuse
        # 会系统性丢 trace，"这一天花了多少"只能从 PG 数。
        with collect_usage() as usage:
            reply = await build_day_page_runner().run(
                [
                    Message(
                        role=Role.USER,
                        content=_day_page_prompt(
                            day=day, lines=lines, previous=previous
                        ),
                    )
                ],
                prompt_vars=prompt_vars,
                context=context,
                max_retries=1,
            )

        await record_round_cost(
            lane=lane,
            actor=persona_id,
            round_id=f"day-page:{day.isoformat()}",
            usage=usage,
            observed_at=now.isoformat(),
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
            written_at=now,
            happenings=len(lines),
        )
        await insert_idempotent(page)
        return page


class DayPageTick(Data):
    """写日记那一拍。单字段 ``ts``——框架源循环固定按 ``data_type(ts=<iso>)`` 造
    payload，多一个必填字段就是每一拍 ValidationError **直接杀 Pod**。"""

    ts: Annotated[str, Key]

    class Meta:
        transient = True


@node
async def day_page_tick(tick: DayPageTick) -> None:
    """到点了让三个人各自把昨天写成一页；不在窗口里的拍什么都不做。

    **并发跑，一个人炸不拖累另两个**（同 :func:`app.living.moment.life_moment_tick`）：
    三条轴各有自己的占用，并发没有竞争。异常不往上抛——源循环那一拍失败会连累另外两
    个人，而下一拍五分钟后就来了，窗口里还有的是机会。
    """
    lane, now = living_lane(), now_cst()
    outcomes = await asyncio.gather(
        *(
            write_day_page(lane=lane, persona_id=persona_id, now=now)
            for persona_id in LIVING_PERSONAS
        ),
        return_exceptions=True,
    )
    for persona_id, outcome in zip(LIVING_PERSONAS, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning(
                "living day page lane=%s persona=%s 这一页炸了：%r",
                lane,
                persona_id,
                outcome,
                exc_info=outcome,
            )
        elif outcome is not None:
            logger.info(
                "living day page lane=%s persona=%s 写下了 %s（材料 %d 行）：%s",
                lane,
                persona_id,
                outcome.day.isoformat(),
                outcome.happenings,
                outcome.text,
            )
