"""每周回看 —— 她读上一周自己写的那几页，重写一版「我是谁」。

跨天沉淀（:mod:`app.living.day_page`）让她记得住昨天。这一层管的是另一件事：**她
会因为过去这一周而变**。写下的那一版进「她是谁」那条版本链（persona 服务，
:mod:`app.living.persona`），而链上最新一版就是她每一轮、写日记时读到的自己
（:meth:`app.living.services.Persona.prompt_vars`）——所以这里落库的东西下一分钟就在
她眼前，不是一份存档。

模块名和 ``source='review'`` 这个值都不许改名：prod 那条链上已经写着几十版
``source='review'``，:meth:`~app.living.services.Persona.has_review_this_week` 认的就
是这个字符串。改了它看不见那些历史版本，她会当场重写一版。

**外部锚是这一轮最要紧的机制。** 她每周基于自己上一版改自己：v(n+1) 的输入里有
v(n)，v(n) 的输入里有 v(n-1)。没有一个不参与这条循环的参照物，几个月之后链上那份
「我是谁」跟她本来是谁已经没有关系了——而且**没有任何报错**，每一版单独读都通顺。
所以 prompt 变量 ``{{persona_core}}`` 在这一轮装的是 ``bot_persona.persona_core``
**原始那一列**，它写死在库里、没有任何代码往里写，是唯一不随她漂的东西。

**⚠️ 同名不同值，是刻意的。** :meth:`~app.living.services.Persona.prompt_vars` 里那个
``{{persona_core}}`` 装的是**链上最新一版**（那是对的：每一轮她该读到现在的自己），
这一轮装的是**扁平列**（那也是对的：重写自己的时候需要一个循环外的参照）。两个变量
同名不同值，因为它们答的是两个不同的问题：「她现在是谁」和「她本来是谁」。所以这一
轮**不复用** ``prompt_vars``，取的是另一份（:meth:`~app.living.services.Persona.review_vars`）——
把它们"统一"掉的后果是锚消失、她开始自我回流，而这件事一句报错都不会有。守这条的
是 ``tests/living/test_persona_review.py`` 里断言 ``prompt_vars["persona_core"]``
等于扁平列原文的那条。

窗口是 CST ``[06:00, 08:00)``，排在写日记（04:00–06:00）之后：上一周最后一天（周日
那个生活日）的页正好是今早写下的，06:00 之后它一定在。早于它就会少一天，而少的正是
最近那天。

**这一轮不给她任何工具**（同 day_page）：她的回复正文**就是**新那一版身份正文，于是
"调了工具却没写成"这整类失败根本不存在。正文 strip 后为空 = 这一轮没成，不落版本
——拿一版空白把这周记成"写过了"是最坏的一种，因为这一周从此永远丢了。

prompt 在 Langfuse（:data:`PERSONA_REVIEW_PROMPT_ID`），变量只有 persona 那两个；上
一周的日记原文和她当前那一版走 USER 消息——它们每周都变，而 prompt 变量改名会**静默**
渲染成字面量。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.agent.contracts import AgentSpec, Segment
from app.agent.core import Agent
from app.agent.neutral import Message, Role
from app.agent.prompts import PromptSet
from app.agent.round import open_call
from app.agent.trace import collect_usage
from app.domain.thinking_cost import record_round_cost
from app.infra.cst_time import CST
from app.living.common import week_start_cst
from app.living.day_page import LivingDayPage, read_day_pages_between
from app.living.services import Persona, PersonaText

logger = logging.getLogger(__name__)

# 回看上一周的窗口，左闭右开（CST）。左边界排在写日记那个窗口（04:00–06:00）之后：
# 上一周最后一天（周日那个生活日）的页是今早写下的，06:00 之前去读会正好少掉最近那
# 一天。右边界给两个钟头，是留给"这一拍没写成"的余地——模型瞬时失败、她一个字都没
# 写，窗口里后面每一拍还会再来。
#
# **这是个每天都开的钟点窗口，不是"周一那两个钟头"。** 周一早上服务没跑（部署、崩
# 溃、机器没开），周二 06:00 那一拍照样会跑，取的仍然是同一个上一个完整周
# （:func:`last_full_week`）——这一周的班漏不掉。这一周写成之前每天的窗口都会再试，
# 写成了就停（"本周已经有 review 版"那道判据）；过了周日还没写成，下一周回看的是刚过去
# 的那一周，没写成的那一周就放弃了。
PERSONA_REVIEW_FROM = time(6, 0)
PERSONA_REVIEW_UNTIL = time(8, 0)

# Langfuse prompt id。
PERSONA_REVIEW_PROMPT_ID = "living_persona_review"

# offline-model：一周三次（三个人各一次），比 day_page 还低一个量级，不该占 life
# 那条高频线的档位。
#
# recursion_limit 1：这一轮**没有工具**，一次生成就完。理由同 day_page —— 有工具的话
# "她调了工具但正文写空了"是一种既没报错、也没有新版本的状态；这里她的回复正文就是
# 新那一版，写没写成只有一个判据。
#
# 一个人的每周回看在 langfuse 里读成一条流（session），逐周翻起来才不用大海捞针。
PERSONA_REVIEW_AGENT = AgentSpec(
    segments=(PERSONA_REVIEW_PROMPT_ID,),
    model="offline-model",
    trace_name="living-persona-review",
    recursion_limit=1,
    session="living-persona-review:{lane}:{participant}",
)
PERSONA_REVIEW_SEGMENT = Segment(
    PERSONA_REVIEW_PROMPT_ID,
    PERSONA_REVIEW_PROMPT_ID,
    variables=("persona_name", "persona_core"),
)


def last_full_week(now: datetime) -> tuple[date, date]:
    """上一个**完整**自然周的 ``[起, 止)``（两个 ``date``，右开）。

    右开界就是 ``now`` 所在那一周的周一（:func:`app.living.common.week_start_cst`），
    所以周一早上跑的时候取到的是上周一到上周日那七天，本周一天都不进来。**不是"最近
    七天"**：滑动窗口下每次跑覆盖的日子都不一样，同一天会被两次回看各读一遍，而她读
    到的"这一周"跟她自己感觉到的一周对不上。

    补跑（比如周三才跑成）取的仍然是同一个完整周，不会跟着当天滑走。
    """
    this_monday = week_start_cst(now).date()
    return this_monday - timedelta(days=7), this_monday


async def week_material(
    *, lane: str, persona_id: str, since: date, until: date
) -> list[LivingDayPage]:
    """这一周她写下的那几页，按日子升序；一页都没有返回空列表。

    **原文照搬，中间没有第二次概括**——理由同 :func:`app.living.day_page.day_material`：
    这一层存在的意义就是让她读自己真的写下的东西，在摆材料这一步先压一遍等于把被否掉
    的"机器折叠原文"从后门放回来。

    这里不做任何裁剪：一页日记本来就是她自己的视角，写的是她自己的经历
    （:func:`app.living.day_page.day_material`）。
    """
    return await read_day_pages_between(
        lane=lane, persona_id=persona_id, since=since, until=until
    )


def _persona_review_prompt(
    *, since: date, until: date, current: str, pages: list[LivingDayPage]
) -> str:
    """摆到她眼前的那一段：上一周是哪几天、她现在对自己的那段描述、那一周的每一页。

    当前那一版给**原文**：她要重写的就是它，不给的话她是凭空写一份而不是"改"，而
    prompt 里说的"重写整段"会变成一句她无从执行的话。

    页与页之间用日子隔开、按顺序摆：一周是有先后的，混成一坨她读到的是七天的碎片。
    """
    diary = "\n\n".join(f"{p.day.strftime('%m-%d')}\n{p.text}" for p in pages)
    return (
        f"刚过去的这一周是 {since.strftime('%Y-%m-%d')} 到 "
        f"{(until - timedelta(days=1)).strftime('%Y-%m-%d')}。\n\n"
        f"你现在对自己的那段描述：\n{current}\n\n"
        f"你这一周每天写下的那几页：\n\n{diary}"
    )


@dataclass(frozen=True)
class DueReview:
    """判出来要做的那一次回看：哪一周、她这一轮的『现在』、那一周她写下的那几页。"""

    # 为什么做这一次回看，记进日志。
    reason: str
    lane: str
    persona_id: str
    # 上一个完整自然周的 ``[起, 止)``（:func:`last_full_week`）。
    since: date
    until: date
    # 她这一轮的『现在』：轮到她时读的钟。
    now: datetime
    # 那一周她写下的那几页，按日子升序（:func:`week_material`）。
    pages: tuple[LivingDayPage, ...]


async def persona_review_due(
    *,
    lane: str,
    persona_id: str,
    at: datetime,
    clock: Callable[[], datetime],
    persona: Persona,
) -> DueReview | None:
    """回看的钟在 ``at`` 响的那一拍，轮到她的回看队列时：要回看哪一周；不该跑返回 ``None``。

    **先读钟**：轮到她的这一刻就是她这一轮的『现在』，回看哪一周、本周写过没有、这一版什么时候
    写下的都按它。前一次回看挂住时（最长占着她的回看队列 15 分钟），排在后面的那一拍要过一阵才
    轮到，这一版是那时写下的，不是那一拍敲响时。

    **窗口按那一拍响的时刻判**（``at``，换到北京时间），理由同
    :func:`app.living.day_page.day_page_due`：排在后面、轮到时已经过了窗口的那一拍照样写。

    之后两道判断都在模型前面，能不调模型就不调：

      1. **本周已经有 review 版**就返回。这是周级幂等的**全部**依据（
         :meth:`~app.living.services.Persona.has_review_this_week`），不另设标记——标记和版本是
         两个事实，中间崩一次就永久对不上。owner 本周盖过版不算，那是 bezhai 的干预，不该顶掉
         她自己的班。窗口每天都开：周一没跑成，同一周后面几天的窗口会补上；过了周日，``now``
         落进下一周，回看的就是刚过去的那一周，没写成的那一周就放弃了。
      2. **上一周一页日记都没有**就返回：那是服务根本没跑的一周，不该有那一版，更不该为一片空白
         叫一次模型（她会凭空编出一周的变化）。

    判和写在她回看队列上的同一次轮次里（:mod:`app.plugins.living`）：两拍打到同一个人时，各自读到
    "本周还没写"就会双双烧一次模型，最后链上多出一版语义重复的正文（版本链是 append-only，没有
    任何东西会拦第二次）。队列上每一拍都排队，轮到时再判一遍。
    """
    now = clock()
    if not (PERSONA_REVIEW_FROM <= at.astimezone(CST).time() < PERSONA_REVIEW_UNTIL):
        return None
    since, until = last_full_week(now)
    if await persona.has_review_this_week(lane=lane, persona_id=persona_id, now=now):
        return None
    pages = await week_material(lane=lane, persona_id=persona_id, since=since, until=until)
    if not pages:
        logger.info(
            "living persona review lane=%s persona=%s %s 那一周一页日记都没有，不写",
            lane,
            persona_id,
            since.isoformat(),
        )
        return None
    return DueReview(
        reason=f"回看 {since.isoformat()} 那一周",
        lane=lane,
        persona_id=persona_id,
        since=since,
        until=until,
        now=now,
        pages=tuple(pages),
    )


async def write_due_review(due: DueReview, *, persona: Persona) -> PersonaText | None:
    """让她读判出来的那一周（:class:`DueReview`）自己写的那几页，重写一版「我是谁」；没写成
    返回 ``None``。

    **链是空的先 seed 一版**（``source='seed'``，内容是她链空时实际读到的那份）。这不是省钱那类
    判断，是让链上的历史连得上：没有起点那一版的话，v1 就是"某天突然出现的一段正文"，往前没有
    任何东西。

    模型跑完之后还有一道：**正文 strip 为空 = 这一轮没成**。不落版本——下一拍还在窗口里的话她会
    再写一次。落一版空白的后果是这一周被记成"写过了"，而她那一版是空的。

    ``written_at`` 用轮到她时读的那一个 ``now``（归一到 CST），不在写的时候再读一次钟：判据 1 读的
    ``written_at`` 和这里写的 ``written_at`` 必须是同一个时刻，两次读钟之间那点差可以正好跨过周界，
    于是同一周写两版、或者下一周的班被当成已经跑过。

    模型的临时失败不整轮重来（:data:`PERSONA_REVIEW_AGENT` 的 ``max_retries`` 是 1）：重放白花一次
    钱；这一轮本来就一周一次，五分钟后那一拍再来就行。模型失败往外抛，由她回看队列上的钟记
    一条 warning（一个人的失败不拖累另两个人）。
    """
    lane, persona_id, since, until = due.lane, due.persona_id, due.since, due.until
    current = await persona.latest(lane=lane, persona_id=persona_id)
    if current is None:
        await persona.seed_chain(lane=lane, persona_id=persona_id)
        current = await persona.latest(lane=lane, persona_id=persona_id)
        if current is None:
            raise RuntimeError(f"seed 之后链仍然是空的 lane={lane} persona={persona_id}")

    prompt_vars = await persona.review_vars(persona_id=persona_id)
    call = open_call(
        PERSONA_REVIEW_PROMPT_ID, PERSONA_REVIEW_AGENT, lane=lane, participant=persona_id
    )
    prompts = await PromptSet.resolve(
        {PERSONA_REVIEW_PROMPT_ID: PERSONA_REVIEW_AGENT},
        {PERSONA_REVIEW_PROMPT_ID: PERSONA_REVIEW_SEGMENT},
        None,
        participant=persona_id,
    )
    # 用量落 durable PG，理由同 moment（见 app.agent.trace）：langfuse
    # 会系统性丢 trace，"这一周花了多少"只能从 PG 数。
    with collect_usage() as usage:
        reply = await Agent(PERSONA_REVIEW_AGENT).run(
            [
                Message(
                    role=Role.USER,
                    content=_persona_review_prompt(
                        since=since,
                        until=until,
                        current=current.narrative,
                        pages=list(due.pages),
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
        round_id=f"persona-review:{since.isoformat()}",
        usage=usage,
        observed_at=due.now.isoformat(),
    )

    written = reply.text().strip()
    if not written:
        logger.warning(
            "living persona review lane=%s persona=%s %s 这一轮一个字都没写，不落版本",
            lane,
            persona_id,
            since.isoformat(),
        )
        return None

    await persona.write_version(
        lane=lane,
        persona_id=persona_id,
        narrative=written,
        source="review",
        written_at=due.now.astimezone(CST).isoformat(),
    )
    # 读回来而不是就地拼一个：返回的那一版要带真实的 ``version``，而版本号是
    # ``insert_append`` 在库里定的。这次读仍在她的回看队列上，读到的一定是刚写的那版。
    version = await persona.latest(lane=lane, persona_id=persona_id)
    if version is not None:
        logger.info(
            "living persona review lane=%s persona=%s 重写了一版（v%d）：%s",
            lane,
            persona_id,
            version.version,
            version.narrative,
        )
    return version
