"""world 反思环节 — 慢钟的写入者：无会话、每日一次、对表翻页（Task 2b）.

「续写场景」和「质疑前提」是相互拮抗的两种姿态——塞进同一次推演里，续写的惯性
永远赢（coe 实证：world 手握现实日期、常识、长弧引导三样线索，仍把过期底色结晶
进了长弧 v1）。所以翻页能力从续写剥离，归这个独立的反思环节：

  * **无会话**：每次从证据现判、不背叙事惯性——``Agent.run`` 不传 session_id
    （不续接 transcript）。langfuse 归组仍用 world 当天的 session id（走
    ``AgentContext.session_id``，只做 trace 标签、不触发续接）。
  * **每日一次**：engine 在 ``WorldState.arc_reflected_date != 今天``（含 None=
    冷启动 / 部署后首跑）时、续写之前调 :func:`run_arc_reflection`；反思**成功**
    才落当日标记（:func:`app.world.state.mark_arc_reflected`），失败不落 → 同日
    后续轮自动重试，一天的机会不被吞掉（spec 决策 5）。
  * **对表翻页**：输入是四样带时间标注的证据——现实此刻+今天日期星期、长弧现状
    及其 turned_at、最新 detail 及其写入时刻、今日底料及其 date / fetched_at
    （缺失如实说）。没有时间标注，无会话的反思无从判断「手里这份快照已经陈旧了
    多少」（spec 决策 4）。
  * **工具集物理隔离**：只有 update_arc（:data:`~app.world.tools.WORLD_REFLECT_TOOLS`）
    ——反思无手碰 detail / notify / sense / sleep，续写无手碰长弧。
  * **fail-open**：反思抛错只记 error 日志、绝不向上抛——当轮续写照常（用反思前
    的长弧也只是旧一天，下轮重试）。update_arc 已 durable 落库而反思 Agent 随后
    失败时，续写仍读到新长弧（engine 在反思之后**现读**长弧）。
  * **durable 写失败 = 整次反思失败**：update_arc 故意不包 @tool_error——
    write_world_arc 抛错照实穿透、炸掉 ``Agent.run``，走上面的 fail-open（不落
    标记、同日重试）。否则写库失败会被包成 tool result 字符串、run 正常返回、
    假成功落标记，同日重试被吃掉。「没调 update_arc」（对完表判断没翻页）则是
    合法成功，照常落标记。
  * **durable 副作用边界同续写**：``max_retries=1``——update_arc 是 durable 写，
    整轮重放会重放它（append 语义相同的版本无害，但不主动制造）。

哪页该翻由反思推演自主判断（prompt 层约束粒度），这里没有翻页检测器 / 频率
限制器（赤尾宪法：不用确定性规则替 agent 决策）。世界底色（这家人是谁）由
langfuse 的 ``world_reflect`` system prompt 自带（与 ``world_deliberate`` 存在
受控重复、两边同批维护）；本模块的 instruction 只承载工具语义与对表任务——代码侧
instruction 是工具语义的权威来源。
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

    对表姿态：把手里的长弧放回现实此刻检查哪页该翻——底料有以底料为准、底料没说
    的用对真实世界的常识加现实日期推；该翻就用 update_arc 整篇重写当前仍成立的
    长弧（翻过去的页被取代不被追加）；明令禁止叙述场景（那是续写的事）。文案平直
    中文、不硬编任何剧情事实（高考 / 角色名 / 日期都不准出现在这里——宪法）。
    """
    return (
        "你是这个世界的反思环节。你的任务只有一件：对表——把手里的「世界长弧」"
        "放回现实此刻检查：这条长弧今天还成立吗？有没有哪一页已经翻过去了？\n\n"
        "下面给你的每份材料都标了它写下的时刻。先看清楚手里这份是多久之前的，"
        "再判断它放到今天还成不成立。今日底料里说了的，以底料为准；底料没说的，"
        "用你对真实世界的常识加上今天的日期去推。\n\n"
        "如果你判断有页已经翻了（或者长弧还是空白、而你已经能从材料读出这个世界"
        "走到了哪一页），就用 update_arc 整篇重写**当前仍然成立**的长弧——翻过去"
        "的页被新的一页取代、不是排在后面被追加，绝不写成历史流水账。长弧写的是"
        "「跨周月仍然成立的世界进展」，判据一句话：这句话下周还成立吗？\n\n"
        "你不叙述场景、不描画此刻的画面（那是续写的事），也不写情绪和主观解读。"
        "如果对完表确认没有哪页该翻，就什么工具也不调、直接说明不需要翻页。"
    )


def _arc_evidence(arc: WorldArc | None) -> str:
    """长弧现状段：有长弧给全文 + turned_at 时间标注；空弧如实说明并引导写第一版。"""
    if arc is None:
        return (
            "长弧还是空白——还没有人写下这个世界走到了哪一页。请从下面的材料读出"
            "世界现在走到哪，用 update_arc 写下第一版。"
        )
    return f"（这版长弧写于 {arc.turned_at}）\n{arc.narrative}"


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

    无会话的对表场景里，底料自己的抓取时刻就是证据——与长弧 turned_at / detail 的
    world_time 同等待遇（所有快照都带时间标注），反思要能看出这份底料记的是哪一天、
    什么时候抓的。缺失如实说缺失（不读昨天、不冒充事实）。
    """
    if materials is None:
        return "（今天还没有抓到外部底料。）"
    return (
        f"（这份底料记录的是 {materials.date} 的外部事实，抓取于 {materials.fetched_at}）\n"
        f"{materials.briefing}"
    )


def _reflection_messages(
    *,
    now: datetime,
    arc: WorldArc | None,
    snapshot: WorldState | None,
    materials: DailyMaterials | None,
) -> list[Message]:
    """把对表的四样证据拼成**单条 user 消息**（无会话、一次喂全）。

    所有快照都带时间标注（长弧 turned_at / detail 的 world_time / 现实此刻+今天
    日期星期）——反思要能看出「手里这份是多久前的」。缺失的证据如实说缺失，绝不
    冒充。模板文案不硬编任何剧情事实（宪法）。
    """
    today = now.strftime("%Y-%m-%d")
    weekday = _WEEKDAY_CN[now.weekday()]
    user_content = (
        f"{reflect_instruction()}\n\n"
        f"【现实此刻】{now.isoformat()}（今天是 {today}，{weekday}）\n\n"
        f"【世界的长弧·现状】\n{_arc_evidence(arc)}\n\n"
        f"【世界最新的此刻叙述】\n{_detail_evidence(snapshot)}\n\n"
        f"【今天的外部底料】\n{_materials_evidence(materials)}\n\n"
        "对完表：该翻页就用 update_arc 整篇重写当前仍成立的长弧；"
        "不该翻就直接说明不需要翻页、不调任何工具。"
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
    """跑一次反思（对表翻页）。**fail-open：本函数绝不向上抛**。

    engine 在「当日尚未完成反思」（``arc_reflected_date != 今天``）时、续写之前调
    本函数。流程：现读长弧（自己读最新版，不用调用方缓存）→ 拼单条 user 消息 →
    无会话跑反思 Agent（工具只有 update_arc、max_retries=1、context 带与续写同等
    的 lane / round features）→ **成功才**落当日标记（mark_arc_reflected）→ 本次
    LLM token 落 durable PG（actor="world_reflect"，与续写区分）。

    任何一步抛错只记 error 日志：当轮续写照常（engine 在反思之后现读长弧——
    update_arc 已落库而 Agent 随后失败时续写仍读到新长弧）、标记不落、同日后续轮
    自动重试。
    """
    try:
        arc = await read_world_arc(lane=lane)
        messages = _reflection_messages(
            now=now, arc=arc, snapshot=snapshot, materials=materials
        )
        # 与续写同等的工具运行契约：update_arc 从 ambient context 读 world_lane
        # 行动（lane 是机制层的事、不进工具签名）；world_round_id 一并给足（与续写
        # 同一轮）。session_id 只塞 context 做 langfuse 归组标签——**不**传给 run
        # （无会话：不读不写 transcript，每次从证据现判）。
        context = AgentContext(
            session_id=trace_session_id,
            features={
                "world_lane": lane,
                "world_round_id": round_id,
            },
        )
        # max_retries=1：update_arc 是 durable 写，整轮重放会重放它（失败语义命门
        # 同续写）。中途失败直接抛 → 走下面的 fail-open。
        with collect_usage() as usage:
            await Agent(_REFLECT_CFG, tools=WORLD_REFLECT_TOOLS).run(
                messages,
                context=context,
                max_retries=1,
            )
        # 反思成功才落当日标记（失败不落 → 同日后续轮重试）。标记先于成本：标记是
        # 反思成功的记账本体，成本是旁路观测（record_round_cost 内部已 best-effort）。
        await mark_arc_reflected(lane=lane, date=now.strftime("%Y-%m-%d"))
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
