"""每周回看 —— 她回头看上一周别人对她说了什么、她身边发生了什么，重写一版「我是谁」。

跨天沉淀（:mod:`app.living.day_page`）让她记得住昨天。这一层管的是另一件事：**她
会因为过去这一周而变**。写下的那一版进「她是谁」那条版本链（persona 服务，
:mod:`app.living.persona`），而链上最新一版就是她每一轮、写日记时读到的自己
（:meth:`app.living.services.Persona.prompt_vars`）——所以这里落库的东西下一分钟就在
她眼前，不是一份存档。

**她回头看一周的时候怎么想、要看到什么。** 周一早上她回头看刚过去的这一周。她想的不是"我这周
说了什么"：自己说过的话她最熟，一句顺嘴的话说上十遍，回头看时就像"我本来就是这样的人"。她想的
是这一周谁来找过她、跟她说了什么；谁跟她近了、谁冷下来了、谁是新出现的；家里和身边有什么变了；
有没有什么事是这周才开始、或者这周结束了的。这几个问题的答案都在别人那一侧：别人在手机上私聊她
的、在群里点她名的、回她话的，姐妹当面跟她说的，她在身边察觉到的。

所以摆给她的就是这些（:func:`week_evidence`），每一条都是原文，带着什么时候、从哪儿来（【手机】
还是【身边】）、是谁，按天分段、按时间先后排下来：读起来是"这一周我身上发生了什么"，不是一份日志。
另外两样是她现在那一版描述（要改的就是它）和她本来是谁（锚，见下）。她的性格和说话的方式由这两样
定，这一周的材料只说明她的处境和她跟别人的关系变了什么；这条范围写在 prompt 里，不在代码里。

**她自己写的日记页和她自己说的话都不摆。** 日记页是她对那一天的复述，她说的话是她自己的腔调，
拿它们当这一周的证据，等于让她这阵子的口头禅和自称替自己作证。2026-09 prod 上 v14、v15、v18
三版就是这么来的：军训、日料、「审判」这些情节，「嘴毒」「威胁式关心」「本学霸」这些自我描述和
自称，每一个都是先出现在她自己说的话里、当晚进了日记页、下一个周一被回看写进「我是谁」；v18 那
一次 prompt 已经明令不许写口头禅。长期记忆的证据要来自她改写不了的东西：别人发来的原始消息、
别人写下的记录。

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

窗口是 CST ``[06:00, 08:00)``。

**这一轮不给她任何工具**（同 day_page）：她的回复正文**就是**新那一版身份正文，于是
"调了工具却没写成"这整类失败根本不存在。正文 strip 后为空 = 这一轮没成，不落版本
——拿一版空白把这周记成"写过了"是最坏的一种，因为这一周从此永远丢了。

prompt 在 Langfuse（:data:`PERSONA_REVIEW_PROMPT_ID`），变量只有 persona 那两个；上
一周的材料和她当前那一版走 USER 消息——它们每周都变，而 prompt 变量改名会**静默**
渲染成字面量。
"""

from __future__ import annotations

import heapq
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
from app.living.services import Persona, PersonaText, Phone, Received

logger = logging.getLogger(__name__)

# 回看上一周的窗口，左闭右开（CST）。左边界原先排在写日记那个窗口（04:00–06:00）之后，
# 因为那时读的是日记页；现在读的是别人对她说的，周一零点那一周就完整了，06:00 没有动。右边界
# 给两个钟头，是留给"这一拍没写成"的余地——模型瞬时失败、她一个字都没写，窗口里后面每一拍
# 还会再来。
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


# 一周的材料最多摆多少条，手机和身边各算各的。不是截断：摆出来的每一条都是原文，超出的是更早的那些，
# 整条不摆，并且如实告诉她还有几条没摆（:func:`_persona_review_prompt`）。留最近的那些：她周一早上
# 回头看，离现在最近的那几天就是她现在的处境。
#
# 两个数按量级定，不是精心算过的：手机上 prod 最忙的一周（2026-09-14 那一周 akao）别人对她说的是
# 212 条；身边那一路 coe-living 上一个人一天收到一百条上下（2026-10-21：80～104 条，大半是 world
# 告诉她察觉到的）。分开算是因为身边那一路一天就上百条，合在一起数的话，它会把真人在手机上对她说的
# 话挤出去。
WEEK_PHONE_LIMIT = 300
WEEK_NEARBY_LIMIT = 700


@dataclass(frozen=True)
class WeekEvidence:
    """摆给她的那一周：别人对她说的、她身边发生的，每条一行，按时间先后；和因为条数上限没摆的几条。"""

    # ``(时刻, 那一行)``，按时刻升序。那一行不带时刻，摆的时候按天分段、每行前面加钟点。
    heard: tuple[tuple[datetime, str], ...]
    phone_left_out: int
    nearby_left_out: int


def _week_bounds(since: date, until: date) -> tuple[datetime, datetime]:
    """``[since, until)`` 这几天换成时刻：CST 零点到零点，跟她的周界
    （:func:`app.living.common.week_start_cst`）是同一个口径。"""
    return (
        datetime.combine(since, time(0), tzinfo=CST),
        datetime.combine(until, time(0), tzinfo=CST),
    )


async def week_evidence(
    *,
    lane: str,
    persona_id: str,
    since: date,
    until: date,
    phone: Phone,
    received: Received,
) -> WeekEvidence:
    """``[since, until)`` 这一周别人对她说的、她身边发生的，每条一行，按时间先后。

    两路，各用各的服务，各是各的样子：

      * 【手机】别人在手机上对她说的：私聊里别人发的，群里点了她名、回了她话的（``phone`` 服务，
        :meth:`~app.living.services.Phone.said_to_her`）。一行是哪条会话、怎么找到她的、谁说的和原话。
      * 【身边】传到她这里的：姐妹当面跟她说的、world 告诉她察觉到的（``received`` 服务）。一行跟她
        每一轮读到的是同一个样子（:meth:`~app.living.services.Received.line`）：姐妹的话带着名字，
        world 的察觉原样摆。

    她自己说的话、她写的日记页都不在里面，理由见模块说明。

    **原文照搬，中间没有第二次概括**，单条不截；条数超过上限时，每一路各留最近的那些
    （:data:`WEEK_PHONE_LIMIT`、:data:`WEEK_NEARBY_LIMIT`），没摆的条数如实交回。

    两路各自已经按时刻排好（手机按消息的时刻，身边按消息自带的时间：world 的告知和姐妹的话走两条
    路，到达先后不代表发生先后），按时刻交错合成一条；时刻相同的，手机上的排在前面。
    """
    start, end = _week_bounds(since, until)
    said = await phone.said_to_her(persona_id=persona_id, since=start, until=end)
    letters = await received.between(
        lane=lane, persona_id=persona_id, since=start, until=end
    )
    said_shown = said[-WEEK_PHONE_LIMIT:]
    letters_shown = letters[-WEEK_NEARBY_LIMIT:]
    heard = heapq.merge(
        [(s.at, f"【手机】{phone.said_to_her_line(s)}") for s in said_shown],
        [(m.message_time, f"【身边】{received.line(m)}") for m in letters_shown],
        key=lambda item: item[0],
    )
    return WeekEvidence(
        heard=tuple(heard),
        phone_left_out=len(said) - len(said_shown),
        nearby_left_out=len(letters) - len(letters_shown),
    )


def _week_text(evidence: WeekEvidence) -> str:
    """那一周按天分段：每段头上是日子，下面每行前面是钟点（CST）。一周是有先后的，混成一坨她读到的是
    七天的碎片；钟点让她看得出一件事和另一件事隔了多久。"""
    days: list[list[str]] = []
    current: date | None = None
    for at, line in evidence.heard:
        local = at.astimezone(CST)
        if local.date() != current:
            current = local.date()
            days.append([local.strftime("%m-%d")])
        days[-1].append(f"- {local.strftime('%H:%M')}{line}")
    return "\n\n".join("\n".join(day) for day in days)


def _persona_review_prompt(
    *, since: date, until: date, current: str, evidence: WeekEvidence
) -> str:
    """摆到她眼前的那一段：上一周是哪几天、她现在对自己的那段描述、那一周别人对她说的和她身边发生的。

    当前那一版给**原文**：她要重写的就是它，不给的话她是凭空写一份而不是"改"，而
    prompt 里说的"重写整段"会变成一句她无从执行的话。

    因为条数上限没摆的，开头如实说还有几条：不说的话她会以为那一周就这么安静。
    """
    left_out = [
        f"{where} {n} 条"
        for where, n in (
            ("手机上", evidence.phone_left_out),
            ("身边", evidence.nearby_left_out),
        )
        if n
    ]
    note = (
        f"（更早的还有 {evidence.phone_left_out + evidence.nearby_left_out} 条没摆在这里："
        f"{'、'.join(left_out)}。）\n\n"
        if left_out
        else ""
    )
    return (
        f"刚过去的这一周是 {since.strftime('%Y-%m-%d')} 到 "
        f"{(until - timedelta(days=1)).strftime('%Y-%m-%d')}。\n\n"
        f"你现在对自己的那段描述：\n{current}\n\n"
        "这一周别人对你说的、你身边发生的，按时间先后。"
        "【手机】是手机上私聊你、在群里点你名或回你话的，"
        "【身边】是你身边发生的、姐妹当面跟你说的：\n\n"
        f"{note}{_week_text(evidence)}"
    )


@dataclass(frozen=True)
class DueReview:
    """判出来要做的那一次回看：哪一周、她这一轮的『现在』、那一周别人对她说的和她身边发生的。"""

    # 为什么做这一次回看，记进日志。
    reason: str
    lane: str
    persona_id: str
    # 上一个完整自然周的 ``[起, 止)``（:func:`last_full_week`）。
    since: date
    until: date
    # 她这一轮的『现在』：轮到她时读的钟。
    now: datetime
    # 那一周别人对她说的、她身边发生的（:func:`week_evidence`）。
    evidence: WeekEvidence


async def persona_review_due(
    *,
    lane: str,
    persona_id: str,
    at: datetime,
    clock: Callable[[], datetime],
    persona: Persona,
    phone: Phone,
    received: Received,
) -> DueReview | None:
    """回看的钟在 ``at`` 响的那一拍，轮到她的回看队列时：要回看哪一周；不该跑返回 ``None``。

    **先读钟**：轮到她的这一刻就是她这一轮的『现在』，回看哪一周、本周写过没有、这一版什么时候
    写下的都按它。前一次回看挂住时（最长占着她的回看队列 15 分钟），排在后面的那一拍要过一阵才
    轮到，这一版是那时写下的，不是那一拍敲响时。

    **窗口按那一拍响的时刻判**（``at``，换到北京时间），理由同
    :func:`app.living.day_page.pages.day_page_due`：排在后面、轮到时已经过了窗口的那一拍照样写。

    之后两道判断都在模型前面，能不调模型就不调：

      1. **本周已经有 review 版**就返回。这是周级幂等的**全部**依据（
         :meth:`~app.living.services.Persona.has_review_this_week`），不另设标记——标记和版本是
         两个事实，中间崩一次就永久对不上。owner 本周盖过版不算，那是 bezhai 的干预，不该顶掉
         她自己的班。窗口每天都开：周一没跑成，同一周后面几天的窗口会补上；过了周日，``now``
         落进下一周，回看的就是刚过去的那一周，没写成的那一周就放弃了。
      2. **上一周没有一个人对她说过话、她身边什么都没传来**就返回（:func:`week_evidence` 一行都
         没有）：不该有那一版，更不该为一片空白叫一次模型（她会凭空编出一周的变化）。她自己写了
         日记、自己说了很多话也一样，那不是这一周发生在她身上的事。

    判和写在她回看队列上的同一次轮次里（:mod:`app.living.persona_review`）：两拍打到同一个人时，各自读到
    "本周还没写"就会双双烧一次模型，最后链上多出一版语义重复的正文（版本链是 append-only，没有
    任何东西会拦第二次）。队列上每一拍都排队，轮到时再判一遍。
    """
    now = clock()
    if not (PERSONA_REVIEW_FROM <= at.astimezone(CST).time() < PERSONA_REVIEW_UNTIL):
        return None
    since, until = last_full_week(now)
    if await persona.has_review_this_week(lane=lane, persona_id=persona_id, now=now):
        return None
    evidence = await week_evidence(
        lane=lane,
        persona_id=persona_id,
        since=since,
        until=until,
        phone=phone,
        received=received,
    )
    if not evidence.heard:
        logger.info(
            "living persona review lane=%s persona=%s %s 那一周没有人对她说过话、身边也什么都没传来，不写",
            lane,
            persona_id,
            since.isoformat(),
        )
        return None
    return DueReview(
        reason=f"回看 {since.isoformat()} 那一周（{len(evidence.heard)} 条）",
        lane=lane,
        persona_id=persona_id,
        since=since,
        until=until,
        now=now,
        evidence=evidence,
    )


async def write_due_review(due: DueReview, *, persona: Persona) -> PersonaText | None:
    """让她读判出来的那一周（:class:`DueReview`）别人对她说的、她身边发生的，重写一版「我是谁」；
    没写成返回 ``None``。

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
                        evidence=due.evidence,
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
