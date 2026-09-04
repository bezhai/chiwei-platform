"""把她自己主动说出去的话撤回来。

**她指的是编号，不是内容。** 她发出去的每条消息在快照里长这样：

    - 21:00 CST 你对 bezhai 说：「你去过那家抹茶店吗？」［55f3469bd46c5384a9ce22cb4944b77a］

方括号里那串就是 :attr:`app.living.mouth.SpokenOutbound.outbound_id`，由
:func:`app.living.snapshot._own_line` 印在她眼前（前缀常量见
:data:`app.living.records.OUTBOUND_HAPPENING_PREFIX`，全链只有那一处定义）。所以这只
手收的是那个编号，在她自己那张认领表里**按等值**找。

真人撤消息是**看着那条点的** —— 他手上一直有一个指针。给她印一个编号就是把同一个指
针交给她。上一版收的是原话片段、后端拿它做子串匹配，那是在逻辑层猜她指的是哪一句：
同一句话说过两遍就分不出是哪一次，于是要回问，而回问这一格本身就是那个猜法造出来的。
等值查找下不存在"对上好几条"，所以这里也没有那段代码。

**只能撤自己的。** 认领表是三个人共用的一张表，按 ``lane`` + ``persona_id`` 筛是硬
约束：只按编号找会让一个从别处拿到的编号撤掉姐姐那条链 —— 那是替另一个人做决定，而
且被撤的那个人一个字都感知不到。

**撤回不过会话白名单那道闸。** 别处每一只手都只碰她视野里的会话
（:func:`app.living.phone.reachable_conversations`），这里走的是未过滤的那份
（:func:`app.living.phone.conversation_her_bot_is_in`）：判据只有"bot 还在不在那个会
话里"。她撤的是自己已经发出去的话，用的是自己那张认领表上的编号（模型编不出别人
的），跟"她现在还看不看得见那条会话"是两件事 —— 让一条发出去时在名单里、之后掉出名单
的消息撤不回来，是纯粹的倒退：那句话还挂在真人眼前，而她眼睁睁没办法。

**编号对不上就如实说没有这条，不替她挑。** 对不上的原因有好几种（编错了、那是姐姐那
条、当面说的话本来就没有编号），这一刻分不出是哪一种，所以只说确定的那一件。绝不排个
序取最近的顶上：撤掉一句她没想撤的话，比撤不掉更糟。

**顺序：先 ``emit``，成功之后才写台账。** 反过来的话，台账写下了而撤回没发出去 ——
她下一缝看台账以为自己撤过了，那句话还好端端留在会话里。

**``emit`` 失败的判法跟她开口那侧相反，这是刻意的。** :mod:`app.living.mouth` 那边
"``emit`` 抛错 = 不知道，所以不重发"，理由是**重复发一条消息会打扰真人**。撤回反过
来：重复撤同一条是幂等的、无害的（删一条已经删掉的消息，真人一个字都看不到）。所以
这边抛错就如实说没撤成、台账留空，她想再撤就再撤。两侧的判法不一致不是疏忽，是因为
"重复"这个动作在两边的后果本来就不一样 —— 谁要把它们改成一致，先想清楚这一条。

**``emit`` 成功之后，写台账失败不许往外抛。** 撤回请求已经在路上了，这时候告诉她
"没撤成"是假话，而她会据此以为那句话还在。

**这里有一个明确接受的缺口，别当它不存在。** ``took_back_at`` 没写下去的话，
:mod:`app.living.landing` 那条对账钟从此选不中这条 —— 它的待办是"她按下过撤回、还
没确认撤掉"，而这条看起来像是她从没撤过。于是台账上永远少一笔"这条撤掉了"，日志之
外没有第二条补回来的路。

**她的体验不受影响**：渠道那侧照常撤，公共层那一行的撤回时刻由投递方写，她拿起手机
一样看不到那条。丢的是可观测性，不是正确性。要补上得让对账去扫全部历史开口（而不是
只扫"她按下过撤回"的那些），代价大于收益。外部评审（2026-09-04）指出过这一处，判断
是接受。

**撤回是另一件发生的事，不是把她说过的话改掉。** 真人撤回微信消息，对方也记得你发
过。所以不动那条 speech ``Happening``（改它还会破坏 ``seq`` 的连续前缀），而是单独
落一条 act。落的时刻是她**请求**撤回那一刻，措辞不说"撤掉了" —— 那一刻渠道那边还
没动。

**结果得等下一次。** 撤回是异步的：工具返回时渠道还没撤。为了给她一个当场的答案去
同步等，就是把一整缝卡在网络上。她下次拿起手机自己看得出来 —— 撤掉了的那条就不在
会话历史里了，没撤掉的还在。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from pydantic import Field
from sqlalchemy import text

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.data.session import get_session
from app.domain.safety import Recall
from app.living.happening import record_happening
from app.living.mouth import SpokenOutbound
from app.living.phone import conversation_her_bot_is_in, medium_for
from app.living.records import KIND_ACT
from app.living.scope import moment_scope, note_recorded
from app.living.whereabouts import current_whereabouts
from app.runtime.emit import emit
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_append, select_latest

logger = logging.getLogger(__name__)

# 投递侧原样落进安全终态那一行的字符串（``apps/lark-service`` 的 recall.ts 只透传，
# 不解释）。写清楚是谁按的：跟事后审计判下来的那些 reason 混在一起时分得开。
TAKE_BACK_REASON = "she_took_it_back"

_OUTBOUND_TABLE = _table_name(SpokenOutbound)

# 她自己那条链的最新一版。``outbound_id`` 是自然键的另一半，所以等值至多命中一条链，
# ``ORDER BY ver DESC LIMIT 1`` 取的就是它此刻的样子。
#
# **``persona_id`` 和 ``lane`` 是硬条件，不是过滤优化**：这张表三个人共用，少了它们，
# 一个从别处拿到的编号就能撤掉姐姐那条链。
_HER_LINE_SQL = f"""
SELECT * FROM {_OUTBOUND_TABLE}
 WHERE lane = :lane AND persona_id = :persona_id AND outbound_id = :outbound_id
 ORDER BY ver DESC LIMIT 1
"""


async def her_line(
    *, lane: str, persona_id: str, outbound_id: str
) -> SpokenOutbound | None:
    """她自己说过的、编号是这一个的那条话（最新一版）；没有这条就是 ``None``。

    **不按状态筛。** ``claimed``（交出去了、没等到确认）那条的事实是"可能已经在真人
    眼前"——恰恰是最该能撤的一条。
    """
    async with get_session() as s:
        row = (
            await s.execute(
                text(_HER_LINE_SQL),
                {
                    "lane": lane,
                    "persona_id": persona_id,
                    "outbound_id": outbound_id,
                },
            )
        ).mappings().first()
    if row is None:
        return None
    return SpokenOutbound(**{k: row[k] for k in SpokenOutbound.model_fields})


async def _remember_she_took_it_back(
    *,
    lane: str,
    persona_id: str,
    line: SpokenOutbound,
    where: str,
    medium: str,
    now: datetime,
) -> None:
    """把"她去撤那句话了"落成她自己的一条记录。

    **是 act 不是 speech**：她做了一个动作，没有再说一句话。内容里带上原话，否则她
    下一缝只知道自己撤过什么东西、不知道撤的是哪句。

    措辞停在"去撤"：这一刻渠道那边还没动，写成"撤掉了"就是替渠道宣布了一个还没有的
    结果。

    ``medium`` 跟她说那句话时同一档（手机 / 群聊）：撤回是在手机上按的，坐她旁边的
    姐姐一个字都看不见。落成当面的动作会让姐姐凭空看见她在撤消息。

    **一个字都不许往外抛。** 走到这里撤回已经交出去了，抛出去会被 :func:`tool_error`
    转成"这条撤回没送出去"喂回给她 —— 那是假话。
    """
    happening_id = f"takeback:{line.outbound_id}"
    try:
        await record_happening(
            lane=lane,
            happening_id=happening_id,
            actor=persona_id,
            place=where,
            kind=KIND_ACT,
            content=f"去撤回自己说过的那句「{line.said}」",
            occurred_at=now,
            audience=(),
            medium=medium,
            channel_id=line.channel_id,
        )
        note_recorded(happening_id)
    except Exception:
        logger.warning(
            "living takeback lane=%s outbound=%s 撤回已经交出去了，但这件事没记进"
            "她自己的记录里（她下一缝不知道自己撤过）",
            lane,
            line.outbound_id,
            exc_info=True,
        )


async def _note_took_back(line: SpokenOutbound, *, at: datetime) -> None:
    """把 ``took_back_at`` 这一根轴合到版本链**最新那一版**上。

    这条链上有三个写者：她开口那条路径（认领 → 收口）、
    :mod:`app.living.landing` 那条对账钟（补落地那两列）、和这里。撞版本是常态，所以
    ``insert_append`` 的返回值必须真的被看：输了就重读最新一版、把自己那根轴合上去再
    写，**绝不用手里那份过期的重写** —— 那会把对账已经补上的落地标识抹回 NULL，而那
    一版记的是已知事实。

    走构造函数而不是 ``model_copy(update=...)``：后者在 pydantic v2 上完全跳过校验，
    naive 的 ``took_back_at`` 会从这条缝里溜进 TIMESTAMPTZ 列。

    **一个字都不许往外抛**（同 :func:`_remember_she_took_it_back`）：撤回已经在路上，
    报成失败是假话。
    """
    current = line
    try:
        while True:
            written = await insert_append(
                SpokenOutbound(
                    **{**current.model_dump(), "took_back_at": at}
                ),
                expected_current_ver=current.ver,
            )
            if written == 1:
                return
            latest = await select_latest(
                SpokenOutbound,
                {"lane": current.lane, "outbound_id": current.outbound_id},
            )
            if latest is None:
                # CAS 说这个键上已经有别的版本了，所以这里一定读得回来；真读不回来
                # 只能是有人把整条链删了。没有可以合并的对象，说出来就停。
                logger.error(
                    "living takeback lane=%s outbound=%s 写台账时整条认领链读不回来"
                    "了，这次撤回没留下记录",
                    current.lane,
                    current.outbound_id,
                )
                return
            assert isinstance(latest, SpokenOutbound)
            logger.info(
                "living takeback lane=%s outbound=%s 写台账时版本被人抢先了"
                "（ver %d → %d），合到最新一版上重写",
                current.lane,
                current.outbound_id,
                current.ver,
                latest.ver,
            )
            current = latest
    except Exception:
        # 整个循环一起兜住：读最新一版也可能炸，而它炸了同样不该被说成"撤回失败"。
        logger.warning(
            "living takeback lane=%s outbound=%s 撤回已经交出去了，但台账没写下"
            "她按下撤回那一刻",
            current.lane,
            current.outbound_id,
            exc_info=True,
        )


@tool
@tool_error("这条撤回没送出去")
async def take_back_message(
    message_id: Annotated[
        str,
        Field(
            description=(
                "要撤的那条消息的编号：「你刚做过、说过」里那句话后面方括号里那串，"
                "照抄，别自己编"
            )
        ),
    ],
) -> str:
    """把你自己发出去的一条消息撤回来。

    你发出去的每条消息，在「你刚做过、说过」里那句话后面都跟着一串方括号里的编号
    ——把那串原样抄进来就行，别自己编。当面说的话和你做的事没有编号，也撤不了。

    只能撤**你自己**发出去的话。

    撤回要过一会儿才落到那边：这一步只是去撤，撤没撤掉这一缝还不知道。你下次拿起
    手机就看得出来——撤掉了的那条不在会话里了，没撤掉的还在。

    Args:
        message_id: 那条消息的编号，从「你刚做过、说过」里照抄。

    Returns:
        你去撤了哪一句的一句确认；没有这个编号时一句如实说明。
    """
    lane, now, persona_id, _moment_id = moment_scope()
    outbound_id = message_id.strip()
    if not outbound_id:
        raise ValueError("你没说要撤哪一条：把那条消息后面方括号里那串编号写进来。")

    line = await her_line(
        lane=lane, persona_id=persona_id, outbound_id=outbound_id
    )
    if line is None:
        # fail-loud，绝不挑一条最近的顶上：撤掉一句她没想撤的话，比撤不掉更糟。
        # 对不上的原因这一刻分不出来（编错了、那是姐姐那条、当面说的话本来就没有
        # 编号），所以只说确定的那一件：没有这条。
        raise ValueError(
            f"你没有编号 {outbound_id} 这条消息。这串要从「你刚做过、说过」里那句话"
            f"后面的方括号里照抄 —— 当面说的话没有编号，也撤不了。"
        )

    # **这一处刻意走未过滤的那份可达性**：判据只有"bot 还在不在那个会话里"，不管这条
    # 会话此刻在不在她视野里（:mod:`app.living.whitelist`）。她撤的是自己已经发出去
    # 的话，用的是自己那张认领表上的编号 —— 跟"她现在还看不看得见那条会话"是两件事。
    # 过白名单闸的话，一条发出去时在名单里、之后掉出名单的消息就撤不回来了，而那句话
    # 还挂在真人眼前：那是纯粹的倒退。
    conv = await conversation_her_bot_is_in(
        persona_id=persona_id, channel_id=line.channel_id
    )
    if conv is None:
        # ``channel`` 决定撤回投哪条队列，猜一个就是投到别的渠道去。bot 已经被移出
        # 那个会话的话，撤回本来也做不成，如实说。
        raise ValueError(
            f"「{line.said}」发在一条你现在够不着的会话上，撤不了。"
        )

    # 位置在 ``emit`` **之前**读：它只是给下面那条记录用的，而 ``emit`` 之后的任何
    # 一处抛错都会被 :func:`tool_error` 转成"这条撤回没送出去"，那是假话。
    where = await current_whereabouts(lane=lane, persona_id=persona_id)

    # 顺序钉死：先交出去，成功了才写台账。反过来的话，台账写下了而撤回没发出去，
    # 她会以为自己撤过了。
    #
    # 这里**不吞异常**：撤回重复投是幂等的（删一条已经删掉的消息，真人一个字都看不
    # 到），所以"没撤成"照实说、台账留空，她想再撤就再撤。这跟她开口那侧的判法相反，
    # 理由见模块 docstring —— 那边重复的后果是真人收到两条。
    await emit(
        Recall(
            outbound_id=line.outbound_id,   # 她主动开口那条链，投递侧按它反查公共层
            session_id=None,                # 主动开口没有会话
            trigger_message_id=None,        # 也没有触发它的来源消息
            chat_id=line.channel_id,
            channel=conv.channel,
            reason=TAKE_BACK_REASON,
            lane=lane,                      # sink 拿它当 outbound_context 的 fallback
        )
    )

    # 以下两步都**不往外抛**：撤回已经在路上了，报成失败是假话。
    await _remember_she_took_it_back(
        lane=lane,
        persona_id=persona_id,
        line=line,
        where=where.place if where is not None else "",
        # 撤回是在手机上按的，跟她说那句话时同一档：坐她旁边的姐姐一个字都看不见。
        medium=medium_for(conv.scope),
        now=now,
    )
    await _note_took_back(line, at=now)

    # 只说这一刻确定的那一件：去撤了。撤没撤得掉这一刻还不知道，说成"撤回了"就是编，
    # 而她会据此以为那句话已经不在了。结果不在这一缝里等（那会把她卡在网络上），
    # 她下次拿起手机自己看得见。
    return (
        f"去撤「{line.said}」了。撤没撤得掉这一缝还不知道 —— "
        f"你下次拿起手机就看得出来：撤成了的那条不在会话里，没撤成的还在。"
    )


TAKEBACK_TOOLS = [take_back_message]
