"""会话白名单 —— 哪些会话进她的视野。

她的 bot 挂在两百多个群和几十条私聊里（prod 实测 2026-09-05：akao 名下 259 群 + 69
私聊）。**bot 在不在**回答的是"这条会话她收发得到吗"，回答不了"这条会话跟她有关系
吗"——绝大多数群里没有人在找她，而她每一缝要为它们逐条查库，还得在信封上一条条读
过去。所以这里再收一道：**不在名单里的会话整个不进她视野**，不是"看得见但发不了"。

判据是用户定的那五条，之间是 **or**：

  * 固定加白的那几个群（Dynamic Config，:data:`LIVING_PINNED_GROUPS_KEY`）；
  * 私聊里真人说过的话超过 :data:`DIRECT_TOTAL_OVER` 条（**只有私聊有这一条**）；
  * 近 1h 至少 1 条在叫她 / 近 6h 至少 3 条 / 近 24h 至少 6 条 / 近 7d 至少 15 条
    （:data:`IN_SIGHT_TIERS`，群和私聊同一套）。

**or 关系是这套规则不锁死人的原因。** 名下只有三五条会话的 persona 够不到任何"攒够
量"的门槛，但有人跟她说话就命中时间窗；第一次私聊她的人第一句话就命中 1h 那一档，
她回得了。这不是漏，是这几条规则本来的样子。

**这是可达性护栏，不是替她做决定。** 名单只决定哪些会话摆到她眼前；摆上来之后她看
不看、回不回、什么时候回，仍然是她自己的判断，这里一个字都不管。

**「在叫她」是 nudge 那条钟已经论证过的两条客观事实**（:mod:`app.living.nudge`）：
私聊本身就意味着有人在等她回，群里点了她的名就是直接叫她。私聊不沿用这一条的话那四
档对私聊恒为 0（私聊里没人 @ 她），"私聊也按上面的规则"就成了空话。判据落在
:func:`app.data.queries.messages.count_summons_since` 上，那一层只数条数、不知道有
"白名单"这回事：阈值、组合、算不出来怎么办都在这里。

**私聊那条永久加白问的是是非题，不是计数题。**
:func:`app.data.queries.messages.find_conversations_others_spoke_in` 回答"别人说过
的话够不够 N 条"，数到第 N 条就停；换成"一共有多少条"就得从第一条数到现在，而这道
判据挂在一分钟一拍的 nudge 钟上、三个人各跑一次（prod 实测 2026-09-05：全历史那种
问法 960ms，是非题 1175 个 buffer 全部命中缓存）。

**阈值写死在代码里，不进配置。** 它们是这个功能的定义，不是运行参数；要改就改代码，
改动本身该被看见。进配置的只有"固定加白哪几个群"——那是剧情事实，会变。

**算不出来 = 不在名单里。** 那两条查询炸了不回退成"当作命中放行"：那道闸会在库最不健
康的时候正好整个消失。同理，固定加白那份配置读不到就当空名单 —— 但**空名单不等于全
哑**，其余四档照常生效（旧的 ``feed_whitelist`` 是"配置挂 = 群聊全静音"，这里不再
来一遍）。

**一缝之内是同一份。** 名单跟"此刻几点"同性质：一缝里多处各算一次，她看到的会话集合
会在一缝中途变化 —— 信封上没有的会话，后半缝突然搜得到、发得出去。所以算过一次就锚
在这一缝的 ambient 状态上（:data:`app.living.scope.FEATURE_IN_SIGHT`），键是"谁 + 哪
一刻"，缝与缝之间不会互相串。nudge 那条钟不在任何一缝里，它现算现用：它算出来的名单
跟随后被唤醒的那一缝是**两次独立计算**，中间隔着模型调用的时间。

**名单只用来收窄视野，不用来放行动作。** 她真要把话发出去时查的是实时的 presence
（:func:`app.living.phone.reachable_conversation` 每次都重新问一遍 bot 还在不在），
锚只决定"这条会话在不在她眼前"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

from inner_shared.dynamic_config import dynamic_config

from app.agent.runtime_context import get_context
from app.data.queries.messages import (
    count_summons_since,
    find_conversations_others_spoke_in,
)
from app.data.queries.persona import (
    find_bot_names_for_persona,
    find_bot_user_ids_for_persona,
)
from app.living.scope import FEATURE_IN_SIGHT

logger = logging.getLogger(__name__)

# Dynamic Config key：值 = JSON 数组，每项是一个群的 ``common_conversation_id``。
# 空 / 没配 = 一个群都不固定加白（**不是全放行**）。
LIVING_PINNED_GROUPS_KEY = "living_pinned_groups"

# 四个时间窗和各自的门槛：(窗口, 至少几条在叫她)。群和私聊同一套。
IN_SIGHT_TIERS: tuple[tuple[timedelta, int], ...] = (
    (timedelta(hours=1), 1),
    (timedelta(hours=6), 3),
    (timedelta(hours=24), 6),
    (timedelta(days=7), 15),
)

# 私聊那条永久加白的门槛：真人说过的话**多于**这个数（用户原话是"大于 30"，所以
# 三十条整不算）。群没有这一条 —— 用户原话里群那边是点名固定加白，不是按量。
DIRECT_TOTAL_OVER = 30


def parse_pinned_groups(raw: str) -> frozenset[str]:
    """把配置串解析成固定加白的那几个群；空 / 坏一律退回空名单。

    **整份要么全对要么全退。** 白名单是可达性边界，半份解析成功比整份失败危险：她会
    在一批说不清为什么的会话里能说话，而配置的人以为自己写的是另一批。坏值打
    warning —— 配置写错了不能静默降级成"这几个群没加白"，那样改配置的人永远不知道
    自己改的东西没生效。

    每一项按 uuid 解析并规范化：库里那一列是 uuid 类型，配置里是人手抄的串，两边不
    落到同一种写法上就是"配置看着没错、这个群一天都没进过名单"，且一句报错都没有。
    """
    if not raw or not raw.strip():
        return frozenset()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise TypeError(f"期望一个数组，拿到 {type(payload).__name__}")
        return frozenset(_parse_pinned(item) for item in payload)
    except Exception as exc:
        logger.warning(
            "dynamic config %s 解析不出会话 id（%s）；本次一个群都不固定加白 —— "
            "其余四档照常生效，去看一眼它的值",
            LIVING_PINNED_GROUPS_KEY,
            exc,
        )
        return frozenset()


def _parse_pinned(item: object) -> str:
    if not isinstance(item, str) or not item.strip():
        raise TypeError(f"每一项要是一个会话 id，拿到 {item!r}")
    return str(uuid.UUID(item.strip()))


async def load_pinned_groups() -> frozenset[str]:
    """读固定加白的那几个群。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread`` 避免缓存
    刷新那一次阻塞事件循环（与 :mod:`app.living.calendar` 同口径）。SDK 拉不到时静默
    返回 default，所以"paas-engine 挂了"和"就是没配"在这里长得一样 —— 两种都当空名单
    处理，这正是 fail-closed 那一侧。
    """
    raw = await asyncio.to_thread(
        dynamic_config.get, LIVING_PINNED_GROUPS_KEY, default=""
    )
    return parse_pinned_groups(raw)


async def channels_in_sight(
    *, persona_id: str, now: datetime, conversations: list[dict]
) -> frozenset[str]:
    """``conversations`` 里此刻在她视野中的那些（``channel_id`` 的集合）。

    ``conversations`` 是调用方已经拿到的那份会话集合（形状同
    :func:`app.data.queries.persona.find_conversations_with_persona_bot` 的出参），
    这里不自己再查一遍 presence。集合为空就是空。

    一缝之内只算一次：算过就锚在这一缝上，同一个人、同一刻再问拿到的是同一份（理由
    见模块 docstring）。缝外面（nudge 那条钟、运维查一眼）现算现用。
    """
    anchored = _anchored(persona_id=persona_id, now=now)
    if anchored is not None:
        return anchored
    settled = await _settle(
        persona_id=persona_id, now=now, conversations=conversations
    )
    _anchor(persona_id=persona_id, now=now, channels=settled)
    return settled


# ---------------------------------------------------------------------------
# 判据本身
# ---------------------------------------------------------------------------


async def _settle(
    *, persona_id: str, now: datetime, conversations: list[dict]
) -> frozenset[str]:
    """按那五条规则算一遍谁在名单里。规则之间是 or，命中一条就够。"""
    if not conversations:
        return frozenset()

    pinned = await load_pinned_groups()
    in_sight = {
        str(c["channel_id"])
        for c in conversations
        # 固定加白**只对群**：私聊那条永久加白是按消息量算的，不是点名的。
        if c["scope"] == "group" and str(c["channel_id"]) in pinned
    }

    bot_user_ids = await find_bot_user_ids_for_persona(persona_id)
    own_bots = await find_bot_names_for_persona(persona_id)
    now_ms = int(now.timestamp() * 1000)
    counted = await _asked(
        count_summons_since,
        conversations=conversations,
        bot_user_ids=bot_user_ids,
        own_bots=own_bots,
        since_ms=[
            now_ms - int(window.total_seconds() * 1000)
            for window, _ in IN_SIGHT_TIERS
        ],
    )
    for row in counted:
        _, enough = IN_SIGHT_TIERS[int(row["window"])]
        if int(row["n"]) >= enough:
            in_sight.add(str(row["channel_id"]))

    # 私聊那条永久加白单独问一次，**而且只问还没进名单的私聊**。
    #
    # 问的是"够不够 31 条"，不是"一共有多少条"：后者要从第一条数到现在，planner 拿不
    # 到有效下界只能整表扫（prod 实测 2026-09-05：960ms、101197 个 buffer，理由写在
    # :data:`app.data.queries.messages._OTHERS_SPOKE_AT_LEAST_SQL` 上），而这条钟一分钟
    # 一拍、三个人各跑一次。规则是"多于 30 条"，闭区间问法就是"至少 31 条"。
    #
    # 单独一次是因为它跟四个时间窗问的不是同一类问题（一个是"这段时间里有几条"，一个
    # 是"够不够"），落在两条查询上；已经命中窗口的私聊不必再问。
    still_out = [
        c
        for c in conversations
        if c["scope"] == "direct" and str(c["channel_id"]) not in in_sight
    ]
    if still_out:
        talked_enough = await _asked(
            find_conversations_others_spoke_in,
            conversations=still_out,
            own_bots=own_bots,
            at_least=DIRECT_TOTAL_OVER + 1,
        )
        in_sight |= {str(row["channel_id"]) for row in talked_enough}
    return frozenset(in_sight)


async def _asked(query, /, **kwargs) -> list[dict]:
    """问库一次；问不出来就当**什么都没数到**。

    fail-closed：拿不到答案就是"这些会话这一刻不在名单里"，不是"当作命中放行"。反
    过来做的话，这道闸会在库最不健康的时候正好整个消失 —— 而那正是最需要它的时候。
    掉出去的代价是她这一缝在那些会话里说不了话，下一缝算得出来就恢复；固定加白那几
    个群不受影响（它们不看计数）。

    四个时间窗和总量那条各是一次查询，共用这一处 —— 各写一遍 try/except 的话迟早出现
    一边堵住、另一边炸了静默放行。
    """
    try:
        return await query(**kwargs)
    except Exception:
        logger.warning(
            "living whitelist 这一刻问不出这些会话跟她有没有关系（%s），本次它们一律"
            "按不在名单里算（她在它们里面说不了话，下一缝再试）",
            getattr(query, "__name__", query),
            exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# 一缝之内的那份锚
# ---------------------------------------------------------------------------


def _anchor_key(persona_id: str, now: datetime) -> str:
    """这份名单是谁的、哪一刻算的。

    带上这两样，锚就是一个**自带前提的答案**而不是一块缓存：换了人或换了时刻问，
    拿到的是重新算的那份，不是上一缝留下的。一缝之内 ``now`` 本来就是同一个（时间
    锚是一缝的身份），所以这一缝里每一处问到的都是同一份。
    """
    return f"{persona_id}\x1f{now.isoformat()}"


def _anchored(*, persona_id: str, now: datetime) -> frozenset[str] | None:
    """这一缝已经定下的那份名单；不在一缝里、或者还没算过返回 ``None``。

    不在一缝里就没有"本缝定下的"这回事（同
    :func:`app.living.phone._pending_cursor`），**不能**跟着
    :func:`app.living.scope.moment_scope` 一起 fail-fast：nudge 那条钟和运维查一眼
    本来就在缝外面。
    """
    try:
        ctx = get_context()
    except LookupError:
        return None
    settled = ctx.features.get(FEATURE_IN_SIGHT)
    if not isinstance(settled, dict):
        return None
    if settled.get("key") != _anchor_key(persona_id, now):
        return None
    return frozenset(settled["channels"])


def _anchor(*, persona_id: str, now: datetime, channels: frozenset[str]) -> None:
    """把这一缝的名单定下来。不在一缝里就什么都不记。"""
    try:
        ctx = get_context()
    except LookupError:
        return
    ctx.features[FEATURE_IN_SIGHT] = {
        "key": _anchor_key(persona_id, now),
        "channels": sorted(channels),
    }
