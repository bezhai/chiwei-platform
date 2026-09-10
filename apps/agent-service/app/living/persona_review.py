"""每周回看 —— 她读上一周自己写的那几页，重写一版「我是谁」。

跨天沉淀（:mod:`app.living.day_page`）让她记得住昨天。这一层管的是另一件事：**她
会因为过去这一周而变**。写下的那一版进 :class:`app.living.persona.PersonaVersion`
版本链，而链上最新一版就是她每一轮、写日记时读到的自己
（:func:`app.living.persona.persona_prompt_vars`）——所以这里落库的东西下一分钟就在
她眼前，不是一份存档。

模块名和 ``source='review'`` 这个值都不许改名：prod 那条链上已经写着几十版
``source='review'``，:func:`~app.living.persona.has_review_version_this_week` 认的就
是这个字符串。改了它看不见那些历史版本，她会当场重写一版。

**外部锚是这一轮最要紧的机制。** 她每周基于自己上一版改自己：v(n+1) 的输入里有
v(n)，v(n) 的输入里有 v(n-1)。没有一个不参与这条循环的参照物，几个月之后链上那份
「我是谁」跟她本来是谁已经没有关系了——而且**没有任何报错**，每一版单独读都通顺。
所以 prompt 变量 ``{{persona_core}}`` 在这一轮装的是 ``bot_persona.persona_core``
**原始那一列**，它写死在库里、没有任何代码往里写，是唯一不随她漂的东西。

**⚠️ 同名不同值，是刻意的。** :func:`app.living.persona.persona_prompt_vars` 里那个
``{{persona_core}}`` 装的是**链上最新一版**（那是对的：每一轮她该读到现在的自己），
这一轮装的是**扁平列**（那也是对的：重写自己的时候需要一个循环外的参照）。两个变量
同名不同值，因为它们答的是两个不同的问题：「她现在是谁」和「她本来是谁」。所以这一
轮**不复用** ``persona_prompt_vars``，自己拼一份（:func:`persona_review_vars`）——
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

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Annotated

from app.agent.context import AgentContext
from app.agent.core import AgentConfig
from app.agent.neutral import Message, Role
from app.agent.trace import collect_usage
from app.capabilities.agent import AgentRunner
from app.data.queries import find_persona
from app.domain.thinking_cost import record_round_cost
from app.infra.cst_time import CST, now_cst
from app.living.clock import living_lane
from app.living.day_page import LivingDayPage, read_day_pages_between
from app.living.persona import (
    LIVING_PERSONAS,
    PersonaVersion,
    has_review_version_this_week,
    read_latest_persona_version,
    seed_persona_chain,
    week_start_cst,
    write_persona_version,
)
from app.living.serial import hold
from app.runtime.data import Data, Key
from app.runtime.node import node

logger = logging.getLogger(__name__)

# 回看上一周的窗口，左闭右开（CST）。左边界排在写日记那个窗口（04:00–06:00）之后：
# 上一周最后一天（周日那个生活日）的页是今早写下的，06:00 之前去读会正好少掉最近那
# 一天。右边界给两个钟头，是留给"这一拍没写成"的余地——模型瞬时失败、她一个字都没
# 写，窗口里后面每一拍还会再来。
#
# **这是个每天都开的钟点窗口，不是"周一那两个钟头"。** 周一早上服务没跑（部署、崩
# 溃、机器没开），周二 06:00 那一拍照样会跑，取的仍然是同一个上一个完整周
# （:func:`last_full_week`）——这一周的班漏不掉。它什么时候停由"本周已经有 review
# 版"那道判据说了算，不由日子说了算。
PERSONA_REVIEW_FROM = time(6, 0)
PERSONA_REVIEW_UNTIL = time(8, 0)

# 钟拍得比窗口密得多，因为"该不该跑"判在节点里（在不在窗口、本周写过没有、上一周有
# 没有日记），一周 2016 拍里绝大多数在打模型之前就返回了。五分钟一拍 = 窗口里有 24
# 次机会。
PERSONA_REVIEW_TICK_SECONDS = 300

# Langfuse prompt id。
PERSONA_REVIEW_PROMPT_ID = "living_persona_review"

# offline-model：一周三次（三个人各一次），比 day_page 还低一个量级，不该占 life
# 那条高频线的档位。
#
# recursion_limit 1：这一轮**没有工具**，一次生成就完。理由同 day_page —— 有工具的话
# "她调了工具但正文写空了"是一种既没报错、也没有新版本的状态；这里她的回复正文就是
# 新那一版，写没写成只有一个判据。
_CFG = AgentConfig(
    PERSONA_REVIEW_PROMPT_ID,
    "offline-model",
    "living-persona-review",
    recursion_limit=1,
)


def persona_review_lock_key(lane: str, persona_id: str) -> str:
    """这个人在这个 lane 上回看那一周的排他占用 key（每人一条轴，互不阻塞）。"""
    return f"living:persona-review:{lane}:{persona_id}"


def last_full_week(now: datetime) -> tuple[date, date]:
    """上一个**完整**自然周的 ``[起, 止)``（两个 ``date``，右开）。

    右开界就是 ``now`` 所在那一周的周一（:func:`app.living.persona.week_start_cst`），
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

    这里不做任何裁剪（谁在场、够不够得着那些在写日记那一步已经裁过了）：一页日记本来
    就是她自己的视角。
    """
    return await read_day_pages_between(
        lane=lane, persona_id=persona_id, since=since, until=until
    )


async def persona_review_vars(*, persona_id: str) -> dict[str, str]:
    """这一轮的两个 prompt 变量：她叫什么，和**她本来是谁**（外部锚）。

    ``persona_core`` 装的是 ``bot_persona.persona_core`` **原始那一列的原文**，不是
    链上最新一版。这跟 :func:`app.living.persona.persona_prompt_vars` 里那个同名变量
    的值不同，是刻意的——见模块 docstring 那一段⚠️：两者答的是「她本来是谁」和「她现
    在是谁」两个不同的问题。这一轮要在自己上一版的基础上改自己，参照物必须站在这条循
    环外面，不然积累的是回声。

    **锚拿不到就 fail fast**（``bot_persona`` 没这行、或者那一列是空白）：这一轮的全
    部意义就是"在锚的约束下改自己"，没有锚还照跑的话，她会在一段空白的参照下重写自
    己，而这件事在库里、在日志里、在她的表现上都看不出来——只有几个月后那份「我是谁」
    已经不是她了。抛出去由 :func:`persona_review_tick` 记一条 warning，这个人这一周
    的班不跑，另外两个照跑。
    """
    persona = await find_persona(persona_id)
    core = getattr(persona, "persona_core", None) or ""
    if not core.strip():
        raise ValueError(
            f"persona_review_vars: bot_persona.persona_core is blank for "
            f"persona_id={persona_id!r} — 这一轮没有外部锚可用，不跑"
        )
    return {
        "persona_name": getattr(persona, "display_name", "") or persona_id,
        "persona_core": core,
    }


def build_persona_review_runner() -> AgentRunner:
    """重写身份正文的 agent。模块级函数，测试替身从这里换掉，不碰真模型。

    **不带任何工具**（见 :data:`_CFG`）：她的回复正文就是新那一版。
    """
    return AgentRunner(_CFG)


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


async def review_persona(
    *, lane: str, persona_id: str, now: datetime
) -> PersonaVersion | None:
    """让她读上一周自己写的那几页，重写一版「我是谁」；这一拍不该跑 / 没写成返回
    ``None``。

    **"本周写过没有"到落库为止在排他占用里**（每人一条轴），理由同 world 的轮次和
    day_page：两条拍打到同一个人时，各自读到"本周还没写"就会双双烧一次模型，最后链
    上多出一版语义重复的正文（版本链是 append-only，没有任何东西会拦第二次）。窗口
    那一道判在占用**外面**：它只看传进来的 ``now``，不读任何共享状态，等锁没有意义
    ——一周 2016 拍里的绝大多数在这里就返回了。

    顺序是"能不调模型就不调"，四道判断全在模型前面：

      1. **不在窗口里**（:data:`PERSONA_REVIEW_FROM` ～ :data:`PERSONA_REVIEW_UNTIL`）
         就返回。日记还没写完、或者这件事早该做完了，都不是回看的时候。
      2. **本周已经有 review 版**就返回。这是周级幂等的**全部**依据（
         :func:`~app.living.persona.has_review_version_this_week`），不另设标记——
         标记和版本是两个事实，中间崩一次就永久对不上。owner 本周盖过版不算，那是
         bezhai 的干预，不该顶掉她自己的班。
      3. **上一周一页日记都没有**就返回：那是服务根本没跑的一周，不该有那一版，更不
         该为一片空白叫一次模型（她会凭空编出一周的变化）。
      4. **链是空的先 seed 一版**（``source='seed'``，内容是她链空时实际读到的那份）。
         这不是省钱那类判断，是让链上的历史连得上：没有起点那一版的话，v1 就是"某天
         突然出现的一段正文"，往前没有任何东西。

    模型跑完之后还有一道：**正文 strip 为空 = 这一轮没成**。不落版本——下一拍还在窗
    口里的话她会再写一次。落一版空白的后果是这一周被记成"写过了"，而她那一版是空的。

    ``written_at`` 用传进来的 ``now``（归一到 CST）而不是现取钟：判据 2 读的
    ``written_at`` 和这里写的 ``written_at`` 必须是同一个时钟，两个时钟之间那点差可
    以正好跨过周界，于是同一周写两版、或者下一周的班被当成已经跑过。生产上钟传的就
    是 ``now_cst()``，值一样。

    ``max_retries=1``：core 的 ``run`` 把整轮包在 ``@retry`` 里，一次模型瞬时失败会
    整轮重放、白花一次钱；这一轮本来就一周一次，五分钟后那一拍再来就行。
    """
    local = now.astimezone(CST)
    if not (PERSONA_REVIEW_FROM <= local.time() < PERSONA_REVIEW_UNTIL):
        return None

    since, until = last_full_week(now)
    async with hold(persona_review_lock_key(lane, persona_id)):
        if await has_review_version_this_week(
            lane=lane, persona_id=persona_id, now=now
        ):
            return None

        pages = await week_material(
            lane=lane, persona_id=persona_id, since=since, until=until
        )
        if not pages:
            logger.info(
                "living persona review lane=%s persona=%s %s 那一周一页日记都没有，不写",
                lane,
                persona_id,
                since.isoformat(),
            )
            return None

        current = await read_latest_persona_version(
            lane=lane, persona_id=persona_id
        )
        if current is None:
            await seed_persona_chain(lane=lane, persona_id=persona_id)
            current = await read_latest_persona_version(
                lane=lane, persona_id=persona_id
            )
            if current is None:
                raise RuntimeError(
                    f"seed 之后链仍然是空的 lane={lane} persona={persona_id}"
                )

        prompt_vars = await persona_review_vars(persona_id=persona_id)
        # 一个人的每周回看在 langfuse 里读成一条流，逐周翻起来才不用大海捞针。没有
        # 工具，所以不需要任何 ambient feature。
        context = AgentContext(
            persona_id=persona_id,
            session_id=f"living-persona-review:{lane}:{persona_id}",
        )
        # 用量落 durable PG，理由同 moment 和 world 轮次（见 app.agent.trace）：langfuse
        # 会系统性丢 trace，"这一周花了多少"只能从 PG 数。
        with collect_usage() as usage:
            reply = await build_persona_review_runner().run(
                [
                    Message(
                        role=Role.USER,
                        content=_persona_review_prompt(
                            since=since,
                            until=until,
                            current=current.narrative,
                            pages=pages,
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
            round_id=f"persona-review:{since.isoformat()}",
            usage=usage,
            observed_at=now.isoformat(),
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

        await write_persona_version(
            lane=lane,
            persona_id=persona_id,
            narrative=written,
            source="review",
            written_at=local.isoformat(),
        )
        # 读回来而不是就地拼一个：返回的那一版要带真实的 ``version``，而版本号是
        # ``insert_append`` 在库里定的。这次读仍在占用里，读到的一定是刚写的那版。
        return await read_latest_persona_version(lane=lane, persona_id=persona_id)


class PersonaReviewTick(Data):
    """每周回看那一拍。单字段 ``ts``——框架源循环固定按 ``data_type(ts=<iso>)`` 造
    payload，多一个必填字段就是每一拍 ValidationError **直接杀 Pod**。"""

    ts: Annotated[str, Key]

    class Meta:
        transient = True


@node
async def persona_review_tick(tick: PersonaReviewTick) -> None:
    """到点了让三个人各自回看上一周；不在窗口里的拍什么都不做。

    **并发跑，一个人炸不拖累另两个**（同 :func:`app.living.day_page.day_page_tick`）：
    三条轴各有自己的占用，并发没有竞争。异常不往上抛——源循环那一拍失败会连累另外两
    个人，而下一拍五分钟后就来了，窗口里还有的是机会。
    """
    lane, now = living_lane(), now_cst()
    outcomes = await asyncio.gather(
        *(
            review_persona(lane=lane, persona_id=persona_id, now=now)
            for persona_id in LIVING_PERSONAS
        ),
        return_exceptions=True,
    )
    for persona_id, outcome in zip(LIVING_PERSONAS, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning(
                "living persona review lane=%s persona=%s 这一版炸了：%r",
                lane,
                persona_id,
                outcome,
                exc_info=outcome,
            )
        elif outcome is not None:
            logger.info(
                "living persona review lane=%s persona=%s 重写了一版（v%d）：%s",
                lane,
                persona_id,
                outcome.version,
                outcome.narrative,
            )
