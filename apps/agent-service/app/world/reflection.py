"""world 反思环节 — 慢钟的写入者：无会话、每日两班、对表翻页 + 交代眼睛.

「续写场景」和「质疑前提」是相互拮抗的两种姿态——塞进同一次推演里，续写的惯性
永远赢（coe 实证：world 手握现实日期、常识、世界阶段引导三样线索，仍把过期底色
结晶进了世界阶段 v1）。所以翻页能力从续写剥离，归这个独立的反思环节：

  * **无会话**：每次从证据现判、不背叙事惯性——``Agent.run`` 不传 session_id
    （不续接 transcript）。langfuse 归组仍用 world 当天的 session id（走
    ``AgentContext.session_id``，只做 trace 标签、不触发续接）。
  * **每日两班（双触发，眼睛闭环）**：world 24×7，每天 00:0X 首轮就会触发反思
    ——那时眼睛还没出门、当天底料不存在，单一标记会让「当天 briefing 永远不被
    当天反思消化」。所以 engine 分两班调 :func:`run_arc_reflection`：第一班照旧
    （``arc_reflected_date != 今天``，无底料也凭常识对表）；第二班在当日底料落地
    后补（底料存在且 ``arc_materials_reflected_date != 今天``）。反思**成功**才落
    标记（:func:`app.world.state.mark_arc_reflected`）：带底料的成功反思**同时落
    两个标记**（它已覆盖两班职责——比如午后部署时首轮就带底料，不冗余跑第二班），
    无底料的只落 ``arc_reflected_date``。失败不落 → 同日后续轮自动重试，一天的
    机会不被吞掉（spec 决策 5）。
  * **对表翻页 + 交代眼睛**：输入是五样带时间标注的证据——现实此刻+今天日期星期、
    世界阶段现状及其 turned_at、最新 detail 及其写入时刻、今日底料及其 date /
    fetched_at、当前关注及其 written_at（缺失如实说）。没有时间标注，无会话的
    反思无从判断「手里这份快照已经陈旧了多少」（spec 决策 4）。对表之外的第二职：
    看今天底料带回了什么（眼睛带着旧关注去看的结果就在底料里），决定接下来还想
    看什么，用 update_attention 整篇重写「当前仍想看的」——不再想看也要重写一版
    说明（append-only 链没有删除态，不写这一版旧关注会被眼睛永远读下去）。
  * **工具集物理隔离**：只有 update_arc + update_attention
    （:data:`~app.world.tools.WORLD_REFLECT_TOOLS`）——反思无手碰 detail / notify /
    sense / sleep，续写与眼睛无手碰世界阶段和关注。
  * **fail-open**：反思抛错只记 error 日志、绝不向上抛——当轮续写照常（用反思前
    的世界阶段也只是旧一天，下轮重试）。update_arc 已 durable 落库而反思 Agent 随后
    失败时，续写仍读到新的世界阶段（engine 在反思之后**现读**世界阶段）。
  * **durable 写失败 = 整次反思失败**：update_arc / update_attention 故意不包
    @tool_error——write_world_arc / write_world_attention 抛错照实穿透、炸掉
    ``Agent.run``，走上面的 fail-open（不落标记、同日重试）。否则写库失败会被包
    成 tool result 字符串、run 正常返回、假成功落标记，同日重试被吃掉。「没调
    工具」（对完表判断没翻页、关注没变）则是合法成功，照常落标记。
  * **durable 副作用边界同续写**：``max_retries=1``——update_arc /
    update_attention 是 durable 写，整轮重放会重放它（append 语义相同的版本无害，
    但不主动制造）。

哪页该翻、还想看什么由反思推演自主判断（prompt 层约束粒度），这里没有翻页
检测器 / 频率限制器（赤尾宪法：不用确定性规则替 agent 决策）。世界底色（这家人
是谁）由 langfuse 的 ``world_reflect`` system prompt 自带（与 ``world_deliberate``
存在受控重复、两边同批维护）；本模块的 instruction 只承载工具语义与对表任务——
代码侧 instruction 是工具语义的权威来源。
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.trace import collect_usage
from app.domain.thinking_cost import record_round_cost
from app.fetch.materials import DailyMaterials
from app.world.arc import (  # module-level so tests can monkeypatch
    WorldArc,
    read_world_arc,
)
from app.world.attention import (  # module-level so tests can monkeypatch
    WorldAttention,
    read_world_attention,
)
from app.world.state import (  # module-level so tests can monkeypatch
    WorldState,
    mark_arc_reflected,
)
from app.world.tools import WORLD_REFLECT_TOOLS

logger = logging.getLogger(__name__)

# 反思的独立 AgentConfig：prompt id 钉为 "world_reflect"（langfuse 上新建、只打
# coe label——发布顺序见 spec 决策 7）。真实环境里 prompt 缺失 = 反思失败 =
# fail-open（当轮续写照常、标记不落、同日重试），这正是设计语义。recursion_limit
# 用默认值：反思是一次对表判断 + 至多一次 update_arc，不需要长工具循环。
_REFLECT_CFG = AgentConfig("world_reflect", "offline-model", "world-reflect")

_WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def reflect_instruction() -> str:
    """喂给反思 Agent 的对表指令（代码侧是工具语义的权威来源）。

    两职：① 对表——把手里的世界阶段放回现实此刻检查哪页该翻——底料有以底料为准、
    底料没说的用对真实世界的常识加现实日期推；该翻就用 update_arc 整篇重写当前仍
    成立的世界阶段（翻过去的页被取代不被追加）。② 交代眼睛——看今天底料带回了什么（眼睛
    带着旧关注去看的结果就在底料里），决定接下来还想看什么，用 update_attention
    整篇重写「当前仍想看的」；不再想看也要重写一版说明当下没有特别要看的
    （append-only 链没有删除态，不写清空版旧关注会被眼睛永远读下去）。关注写
    「想看哪」、世界阶段写「走到哪」，语义不混。明令禁止叙述场景（那是续写的事）。
    文案平直中文、不硬编任何剧情事实（高考 / 角色名 / 日期都不准出现在这里——宪法）。
    """
    return (
        "你是这个世界的反思环节。你有两件事要做。\n\n"
        "第一件：对表。把手里的「世界阶段」放回现实此刻检查：这个阶段今天还成立"
        "吗？有没有哪一页已经翻过去了？下面给你的每份材料都标了它写下的时刻。先看"
        "清楚手里这份是多久之前的，再判断它放到今天还成不成立。今日底料里说了的，"
        "以底料为准；底料没说的，用你对真实世界的常识加上今天的日期去推。\n\n"
        "如果你判断有页已经翻了（或者世界阶段还是空白、而你已经能从材料读出这个世界"
        "走到了哪一页），就用 update_arc 整篇重写**当前仍然成立**的世界阶段——翻过去"
        "的页被新的一页取代、不是排在后面被追加，绝不写成历史流水账。世界阶段写的是"
        "「跨周月仍然成立的世界进展」，判据一句话：这句话下周还成立吗？\n\n"
        "第二件：交代眼睛。这个世界有一双每天清早出门看外面的眼睛，它带着你之前"
        "留下的「当前关注」去看，看到的（或没看到的）就写在今天的底料里。所以对完"
        "表，再看一眼今天的底料带回了什么：之前想看的，看到了吗？看完了还要不要"
        "继续看？有没有新冒出来想确认的事？想清楚后用 update_attention 整篇重写"
        "**当前仍想看的**——看完的、不再关心的被新版取代、不是追加成清单。如果当下"
        "没有什么特别要看的，也要重写一版、说明现在没有特别要看的——不写这一版，"
        "眼睛明早还会带着过时的关注去看。关注写的是「想看哪」，世界阶段写的是「世界"
        "走到哪」，两边不要混。\n\n"
        "你不叙述场景、不描画此刻的画面（那是续写的事），也不写情绪和主观解读。"
        "如果对完表确认没有哪页该翻、关注也不需要变，就什么工具也不调、直接说明。"
    )


def _arc_evidence(arc: WorldArc | None) -> str:
    """世界阶段现状段：有阶段给全文 + turned_at 时间标注；空白如实说明并引导写第一版。"""
    if arc is None:
        return (
            "世界阶段还是空白——还没有人写下这个世界走到了哪一页。请从下面的材料读出"
            "世界现在走到哪，用 update_arc 写下第一版。"
        )
    return f"（这版世界阶段写于 {arc.turned_at}）\n{arc.narrative}"


def _detail_evidence(snapshot: WorldState | None) -> str:
    """最新此刻叙述段：有快照给 detail + world_time 写入时刻标注；没有如实说明。

    「没有」包括两种：无快照（真冷启动），以及 detail 空白的最小占位快照
    （mark_arc_reflected 冷启路径只承载当日标记、不冒充叙述）——都如实说还没有
    世界叙述，绝不渲染成「（这段叙述写于 ）」+ 空文。
    """
    if snapshot is None or not snapshot.detail:
        return "（还没有任何世界叙述——世界还没醒来过。）"
    return f"（这段叙述写于 {snapshot.world_time}）\n{snapshot.detail}"


def _materials_evidence(materials: DailyMaterials | None) -> str:
    """今日底料段：有给 briefing 原文 + date / fetched_at 时间标注；缺失如实说缺失。

    无会话的对表场景里，底料自己的抓取时刻就是证据——与世界阶段 turned_at / detail 的
    world_time 同等待遇（所有快照都带时间标注），反思要能看出这份底料记的是哪一天、
    什么时候抓的。缺失如实说缺失（不读昨天、不冒充事实）。
    """
    if materials is None:
        return "（今天还没有抓到外部底料。）"
    return (
        f"（这份底料记录的是 {materials.date} 的外部事实，抓取于 {materials.fetched_at}）\n"
        f"{materials.briefing}"
    )


def _attention_evidence(attention: WorldAttention | None) -> str:
    """当前关注段：有关注给全文 + written_at 时间标注；没有如实说还没人交代过。

    关注是反思自己此前留给眼睛的「想看哪」——回看它才知道眼睛今天带着什么出的门、
    底料里的回应该对照什么，进而决定续看还是清掉。written_at 与世界阶段 turned_at /
    detail 的 world_time 同等待遇（所有快照都带时间标注）：反思要能看出这版关注
    是哪天留的、是不是已经过时。缺失（从没留过关注）如实说，不冒充——此时眼睛
    只做了本能扫视，要不要留第一版由反思对完表自己判断。
    """
    if attention is None:
        return "（还没有人交代过眼睛要看什么——眼睛此前只做了本能扫视。）"
    return f"（这版关注写于 {attention.written_at}）\n{attention.narrative}"


def _reflection_messages(
    *,
    now: datetime,
    arc: WorldArc | None,
    snapshot: WorldState | None,
    materials: DailyMaterials | None,
    attention: WorldAttention | None,
) -> list[Message]:
    """把对表的五样证据拼成**单条 user 消息**（无会话、一次喂全）。

    所有快照都带时间标注（世界阶段 turned_at / detail 的 world_time / 底料 fetched_at /
    关注 written_at / 现实此刻+今天日期星期）——反思要能看出「手里这份是多久前的」。
    缺失的证据如实说缺失，绝不冒充。模板文案不硬编任何剧情事实（宪法）。
    """
    today = now.strftime("%Y-%m-%d")
    weekday = _WEEKDAY_CN[now.weekday()]
    user_content = (
        f"{reflect_instruction()}\n\n"
        f"【现实此刻】{now.isoformat()}（今天是 {today}，{weekday}）\n\n"
        f"【世界阶段·现状】\n{_arc_evidence(arc)}\n\n"
        f"【世界最新的此刻叙述】\n{_detail_evidence(snapshot)}\n\n"
        f"【今天的外部底料】\n{_materials_evidence(materials)}\n\n"
        f"【当前关注（之前留给眼睛的）】\n{_attention_evidence(attention)}\n\n"
        "对完表：该翻页就用 update_arc 整篇重写当前仍成立的世界阶段，不该翻就不动它；"
        "再对照当前关注看看今天底料带回了什么，关注该变就用 update_attention 整篇"
        "重写（没有要看的就重写一版说明）；两边都不需要动就直接说明、不调任何工具。"
    )
    return [Message(role=Role.USER, content=user_content)]


async def run_arc_reflection(
    *,
    lane: str,
    now: datetime,
    snapshot: WorldState | None,
    materials: DailyMaterials | None,
    round_id: str,
    trace_session_id: str | None,
) -> None:
    """跑一次反思（对表翻页 + 交代眼睛）。**fail-open：本函数绝不向上抛**。

    engine 分两班调本函数（都在续写之前）：第一班「当日尚未完成反思」
    （``arc_reflected_date != 今天``）；第二班「当日底料落地且尚未被反思消化」
    （底料存在且 ``arc_materials_reflected_date != 今天``）。流程：现读世界阶段 + 关注
    （自己读最新版，不用调用方缓存）→ 拼单条 user 消息 → 无会话跑反思 Agent（工具
    只有 update_arc / update_attention、max_retries=1、context 带与续写同等的
    lane / round features）→ **成功才**落标记（mark_arc_reflected）：本次**带底料**
    （``materials`` 非 None）则同落两个标记（这次反思已覆盖两班职责，避免冗余
    第二班），无底料只落 ``arc_reflected_date`` → 本次 LLM token 落 durable PG
    （actor="world_reflect"，与续写区分）。

    任何一步抛错只记 error 日志：当轮续写照常（engine 在反思之后现读世界阶段——
    update_arc 已落库而 Agent 随后失败时续写仍读到新的世界阶段）、标记不落、同日后续轮
    自动重试。
    """
    try:
        arc = await read_world_arc(lane=lane)
        attention = await read_world_attention(lane=lane)
        messages = _reflection_messages(
            now=now, arc=arc, snapshot=snapshot, materials=materials,
            attention=attention,
        )
        # 与续写同等的工具运行契约：update_arc / update_attention 从 ambient
        # context 读 world_lane 行动（lane 是机制层的事、不进工具签名）；
        # world_round_id 一并给足（与续写同一轮）。session_id 只塞 context 做
        # langfuse 归组标签——**不**传给 run（无会话：不读不写 transcript，每次从
        # 证据现判）。
        context = AgentContext(
            session_id=trace_session_id,
            features={
                "world_lane": lane,
                "world_round_id": round_id,
            },
        )
        # max_retries=1：update_arc / update_attention 是 durable 写，整轮重放会
        # 重放它们（失败语义命门同续写）。中途失败直接抛 → 走下面的 fail-open。
        with collect_usage() as usage:
            await Agent(_REFLECT_CFG, tools=WORLD_REFLECT_TOOLS).run(
                messages,
                context=context,
                max_retries=1,
            )
        # 反思成功才落标记（失败不落 → 同日后续轮重试）。带底料的成功反思同落两个
        # 标记（已覆盖两班职责）；无底料只落第一班标记、不碰第二班（白天底料落地后
        # 补班的机会不被吞）。标记先于成本：标记是反思成功的记账本体，成本是旁路
        # 观测（record_round_cost 内部已 best-effort）。
        today = now.strftime("%Y-%m-%d")
        await mark_arc_reflected(
            lane=lane,
            date=today,
            materials_date=today if materials is not None else None,
        )
        await record_round_cost(
            lane=lane,
            actor="world_reflect",
            round_id=round_id,
            usage=usage,
            observed_at=now.isoformat(),
        )
    except Exception:
        logger.error(
            "world 反思环节失败（lane=%s round=%s），fail-open：当轮续写照常、"
            "当日标记不落、同日后续轮自动重试",
            lane,
            round_id,
            exc_info=True,
        )
