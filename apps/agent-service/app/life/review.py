"""睡前回顾本体 — 她自己的慢钟：回看刚结束的生活日，写昨天页 + 关系页.

照 world 反思（:func:`app.world.reflection.run_arc_reflection`）同构：

  * **无会话**：回顾不续接她当天的意识流 transcript（``Agent.run`` 不传
    session_id）——意识流是证据之一、不是叙事底座，从证据现判这一天留下了什么。
    langfuse 归组仍走 ``AgentContext.session_id``（只做 trace 标签）。
  * **单条 user 消息拼证据**，全部带时间标注、缺失如实说：该生活日窗口
    （[04:00, 触发时刻]，见 :mod:`app.life.living_day`）内——她两个自然日的
    意识流（按条目、绝不字符截断）、她做过的 act、她**发过言**的 chat 的原始
    对话（参与边界 = 她发过言，被动在场不算——spec 决策 2b）、当天聊过各人的
    旧关系页、目标生活日已有的昨天页（同日重跑场景）。
  * **fail-open**：任何一步抛错只记 error 日志、绝不向上抛——快班在 life 轮收口
    处调它，绝不杀 life 轮；页不落、清晨对账班按页缺失自动补跑。
  * **max_retries=1**：update_day_page / update_relationship_page 是 durable 写，
    整轮重放会重放它们。工具故意不包 @tool_error（写库失败穿透炸 run → 页不落、
    对账班补，见 review_tools 模块 docstring）。
  * **触发源语义**（2026-06-12 prod 事故修复：单字段 marker 回答不了「某一天
    回顾过没有」）：快班（``trigger="sleep"``）**无闸、每次入睡都回顾当前生活
    日**——午睡 / 回笼觉自然产生中间版，后一次整篇盖前一次（页本就版本叠加、
    读侧取最新版）；对账班（``trigger="sweep"``）锁内权威复查**按页存在性**
    （:func:`app.life.pages.day_page_exists`）——目标日已有页绝不重跑。
  * **昨天页真写了才落 marker**（:func:`app.domain.life_state.mark_day_reviewed`，
    marker 已降级为观测留痕、不再当闸读）：run 正常返回 ≠ 成功——模型可能一个
    工具都没调。run 返回后**现读**昨天页，页存在（本次写的、或更早班写的）才
    落标；页不存在 = 本次回顾失败，不落标、error 留痕、对账班按页缺失自动补。
    关系页不核验（没聊过天就不动关系页是合法的）。成本落 durable PG（actor =
    ``{persona}:day_review``，round_id 从 (lane, persona, target_date, 触发时刻)
    派生——同一天多次合法回顾各自入账、不被幂等去重吞掉），**无论成败都记**
    （token 真烧了）。
  * **整段硬超时**（:data:`DAY_REVIEW_TIMEOUT_SECONDS`，< 单飞锁 TTL）：证据
    收集 / run / 落页核验任何一步挂死都被掐掉，走既有 fail-open。
  * **single_flight 包整段**（key 含 lane / persona / target_date）：快班与
    凌晨补班撞车时冲突方静默让位——锁只防并发撞车，不兼任同日防重跑（快班
    同日重跑是设计行为）。

写什么、给谁重写关系页由她自己判断（写作纪律在 prompt 层：instruction 钉姿态、
langfuse system prompt 载人设）；这里没有内容检测器（赤尾宪法）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Literal

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.session import load_session  # module-level so tests can monkeypatch
from app.agent.session_fold import (
    is_fold_message,
    split_fold_message,
    strip_round_markers,
)
from app.agent.trace import collect_usage, make_session_id
from app.data.message_record import CommonMessageRecord
from app.data.queries.acts import (  # module-level so tests can monkeypatch
    list_persona_acts_between,
)
from app.data.queries.messages import (  # module-level so tests can monkeypatch
    find_persona_spoken_chats_in_window,
)
from app.domain.life_state import (  # module-level so tests can monkeypatch
    mark_day_reviewed,
)
from app.domain.thinking_cost import record_round_cost
from app.domain.world_events import ActPerformed
from app.infra import cst_time
from app.life.living_day import evidence_window, window_session_dates
from app.life.pages import (  # module-level so tests can monkeypatch
    DayPage,
    RelationshipPage,
    day_page_exists,
    read_day_page,
    read_relationship_pages,
)
from app.life.review_tools import (
    FEATURE_REVIEW_LANE,
    FEATURE_REVIEW_PERSONA,
    FEATURE_REVIEW_TARGET_DATE,
    LIFE_REVIEW_TOOLS,
)
from app.memory._persona import load_persona  # module-level so tests can monkeypatch
from app.runtime.single_flight import SingleFlightConflict, single_flight

logger = logging.getLogger(__name__)

# 回顾的独立 AgentConfig：prompt id 钉为 "life_day_review"（langfuse 上由主会话
# 发布、prompt_vars 契约 = {persona_name, persona_lite}）。prompt 缺失 = 回顾失败
# = fail-open（marker 不落、下一班重试），与 world_reflect 同语义。recursion_limit
# 给够：一晚的页是 1 张昨天页 + N 张关系页，工具循环可能多轮。
_REVIEW_CFG = AgentConfig(
    "life_day_review", "offline-model", "life-day-review", recursion_limit=12
)

# 每个 chat 取窗口内消息的条目上限（条目数量控制、绝不字符截断——超限保最近的）。
PER_CHAT_MESSAGE_LIMIT = 50

# 回顾单飞锁 TTL：> 一次回顾的最坏耗时（一次离线模型 run + 多次工具调用，分钟级），
# 同时远 < 两班间隔。TTL 语义见 app/runtime/single_flight.py。
DAY_REVIEW_LOCK_TTL_SECONDS = 600

# 整段回顾（证据收集 + Agent.run + 落页核验）的硬超时。依据：必须 < 单飞锁 TTL
# （600s）——锁 TTL 到期后新班就能拿锁进来，若挂死的旧班还活着会出现两班并发写
# 同一生活日；取 480s 留 120s 安全边距，同时远大于一次离线模型 run + 多次工具
# 调用的正常耗时（分钟级）。超时走既有 fail-open：marker 不落、下一班重试。
DAY_REVIEW_TIMEOUT_SECONDS = 480

# 同一目标生活日的回顾 round_id 派生命名空间种子（与 life 轮的 round_id 空间不混）。
_ROUND_SEED = "life_day_review"


def review_instruction() -> str:
    """喂给回顾 Agent 的任务指令（代码侧是工具语义的权威来源；零剧情事实——宪法）。"""
    return (
        "今天就要过完了。以你本人的第一人称，回看下面这一天的全部经历——你的"
        "意识流、你做过的事、你和人聊过的天。然后做两件事。\n\n"
        "第一件：用 update_day_page 写下这一天**留下来的几笔**。不是把一天按时间"
        "顺序复述一遍的流水账，而是这一天过完、心里还剩下什么：触动你的事、起伏的"
        "瞬间、悬着没落地的念头。只写真实经历过的，绝不编没发生的事。每次调用都是"
        "整篇重写这一天的页：新的一版取代旧版。\n\n"
        "第二件：为今天**真正聊过天的每个真人**，用 update_relationship_page 整篇"
        "重写你心里「他与我」的那一页——他是个什么样的人、你们之间是什么温度、一起"
        "经历过什么沉下来的事、有什么没聊完的线头、你跟他怎么相处。写关系，不写"
        "档案。拿旧的一页（如果有）和今天的相处现判：新版取代旧版，旧的让位新的、"
        "淡了的自然淡出，**一页之内**，别越写越长。other_user_id 用聊天记录里"
        "标出的那个用户标识。今天**没聊过天就不动关系页**——一页都不写；没和某人"
        "聊过，就不动他那页。只写真实聊过的，不编。\n\n"
        "写页的日期和时刻由系统自动记，你不用管钟。"
    )


def _message_text(record: CommonMessageRecord) -> str:
    """从 record.content（JSON 串）抠纯文本；解析不动就原样回显（不静默吞）。"""
    try:
        return json.loads(record.content).get("text") or record.content
    except (json.JSONDecodeError, AttributeError):
        return record.content


def _transcript_evidence(day_sessions: list[tuple[str, list[Message]]]) -> str:
    """意识流证据：按自然日标注、按条目铺开（USER=她当时的感知、ASSISTANT=她当时所想）。

    工具结果（TOOL role）是机械确认文本（"状态已更新"），不进证据；空文本条目跳过。
    两个自然日都空 → 如实说没有记录。条目级取舍、绝不字符截断。

    机制载荷过滤（折叠铁律③）：round marker 是 turn 幂等的机器标记、不是她的感知，
    绝不能喂给回顾——折叠消息只取沉淀正文（那是她的记忆固化，载荷段整段丢）；
    普通 stimulus 里的 marker 行摘掉后正文照常进证据。
    """
    sections: list[str] = []
    for date, history in day_sessions:
        lines = []
        for m in history:
            if m.role not in (Role.USER, Role.ASSISTANT):
                continue
            if is_fold_message(m):
                sediment, _markers = split_fold_message(m)
                sediment = sediment.strip()
                if sediment:
                    lines.append(f"〔这之前的经历，你记得〕{sediment}")
                continue
            text = strip_round_markers(m.text()).strip()
            if not text:
                continue
            prefix = "你当时感知到" if m.role == Role.USER else "你当时想着 / 说做了"
            lines.append(f"〔{prefix}〕{text}")
        if lines:
            sections.append(f"（{date} 这个自然日）\n" + "\n".join(lines))
    if not sections:
        return "（这一天没有留下意识流记录——这段时间你没怎么醒来想事。）"
    return "\n\n".join(sections)


def _acts_evidence(acts: list[ActPerformed]) -> str:
    """act 证据：每件带发生时刻（CST 显示）；这一天没做过事如实说。"""
    if not acts:
        return "（这段时间没有留下你做过事的记录。）"
    return "\n".join(
        f"（{cst_time.to_cst_hms(act.occurred_at)}）{act.description}" for act in acts
    )


def _chats_evidence(
    chats: list[tuple[str, str | None, list[tuple[CommonMessageRecord, str | None]]]],
    *,
    persona_id: str,
) -> str:
    """聊天证据：按 chat 分组、每条带时刻 + 说话人身份（她自己标"你说"）。

    对方的用户标识（user_id）显式标出——关系页的 other_user_id 用它，不让模型
    从名字猜。没聊过天如实说（关系页这次不动的依据）。
    """
    if not chats:
        return "（这一天你没有和任何人聊过天。）"
    blocks: list[str] = []
    for _chat_id, chat_name, entries in chats:
        lines = []
        for record, speaker_persona in entries:
            stamp = cst_time.to_cst_hms(str(record.create_time))
            text = _message_text(record)
            if speaker_persona == persona_id:
                lines.append(f"（{stamp}）你说：{text}")
            elif speaker_persona:
                lines.append(f"（{stamp}）{speaker_persona} 说：{text}")
            elif record.user_id:
                who = record.username or "（不知名）"
                lines.append(f"（{stamp}）{who}（用户标识 {record.user_id}）说：{text}")
            else:
                lines.append(f"（{stamp}）{record.username or '（不知名）'} 说：{text}")
        header = f"〔{chat_name or '（未命名对话）'}〕"
        blocks.append(header + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _chat_partner_ids(
    chats: list[tuple[str, str | None, list[tuple[CommonMessageRecord, str | None]]]],
) -> list[str]:
    """窗口内真实互动过的真人 user_id（出现序去重；bot / 无标识的不算）。"""
    seen: list[str] = []
    for _chat_id, _name, entries in chats:
        for record, speaker_persona in entries:
            if speaker_persona or record.role != "user":
                continue
            if record.user_id and record.user_id not in seen:
                seen.append(record.user_id)
    return seen


def _old_pages_evidence(
    partner_ids: list[str],
    pages: dict[str, RelationshipPage],
    usernames: dict[str, str],
) -> str:
    """当天聊过各人的旧关系页：有页给全文 + written_at；没页如实说第一次。"""
    if not partner_ids:
        return "（没聊过天，也就没有要回看的关系页。）"
    blocks: list[str] = []
    for user_id in partner_ids:
        name = usernames.get(user_id, "")
        label = f"{user_id}（{name}）" if name else user_id
        page = pages.get(user_id)
        if page is None:
            blocks.append(f"〔{label}〕你心里还没有这个人的页——今天是第一次为他落笔。")
        else:
            blocks.append(
                f"〔{label}，这页写于 {page.written_at}〕\n{page.narrative}"
            )
    return "\n\n".join(blocks)


def _existing_day_page_evidence(page: DayPage | None) -> str:
    """目标生活日已有的昨天页（同日重跑场景）：有给全文 + written_at；没有如实说。"""
    if page is None:
        return "（这一天你还没写过页——这是第一次写。）"
    return (
        f"（你已为这一天写过一版，写于 {page.written_at}——这次整篇重写会取代它）\n"
        f"{page.narrative}"
    )


def _review_messages(
    *,
    now: datetime,
    target_date: str,
    day_sessions: list[tuple[str, list[Message]]],
    acts: list[ActPerformed],
    chats: list[tuple[str, str | None, list[tuple[CommonMessageRecord, str | None]]]],
    persona_id: str,
    old_pages: dict[str, RelationshipPage],
    existing_day_page: DayPage | None,
) -> list[Message]:
    """把回顾的全部证据拼成**单条 user 消息**（无会话、一次喂全；模板零剧情事实）。"""
    partner_ids = _chat_partner_ids(chats)
    usernames: dict[str, str] = {}
    for _chat_id, _name, entries in chats:
        for record, speaker_persona in entries:
            if not speaker_persona and record.user_id and record.username:
                usernames.setdefault(record.user_id, record.username)

    user_content = (
        f"{review_instruction()}\n\n"
        f"【现实此刻】{now.isoformat()}\n"
        f"【你回看的这一天】{target_date}（从那天凌晨四点起、到此刻为止）\n\n"
        f"【这一天你的意识流】\n{_transcript_evidence(day_sessions)}\n\n"
        f"【这一天你做过的事】\n{_acts_evidence(acts)}\n\n"
        f"【这一天你的聊天】\n{_chats_evidence(chats, persona_id=persona_id)}\n\n"
        f"【你心里这些人原来的页】\n"
        f"{_old_pages_evidence(partner_ids, old_pages, usernames)}\n\n"
        f"【这一天已有的页】\n{_existing_day_page_evidence(existing_day_page)}\n\n"
        "回看完：用 update_day_page 写下这一天留下来的几笔；为今天真正聊过的每个"
        "真人用 update_relationship_page 整篇重写他那一页；没聊过天就不动关系页。"
    )
    return [Message(role=Role.USER, content=user_content)]


async def run_day_review(
    *,
    lane: str,
    persona_id: str,
    target_date: str,
    now: datetime,
    trace_session_id: str | None,
    trigger: Literal["sleep", "sweep"],
) -> None:
    """跑一次睡前回顾。**fail-open：本函数绝不向上抛**（绝不杀 life 轮 / cron 班）。

    两班都调本函数，由 ``trigger`` 亮明触发源（语义钉死，2026-06-12 事故修复）：

      * ``"sleep"``（快班 = life 轮收口她标了 sleep）：**无闸、永远跑**——同一
        生活日后一次回顾整篇盖前一次（页版本叠加、读侧取最新版），午睡 / 回笼觉
        产生中间版是设计行为。
      * ``"sweep"``（对账班 = 清晨对账 cron）：锁内权威复查**按页存在性**——
        目标日已有页（任何班写的）绝不重跑，页缺失才补（调用方的预检查只是
        省一次锁）。

    整段包 single_flight（key 含 lane / persona / target_date）：撞锁 = 另一班正在
    回顾同一生活日，静默让位（info 留痕）——锁只防并发撞车，不兼任同日防重跑。
    其余任何异常只记 error 日志：页不落、对账班按页缺失自动补。
    """
    lock_key = f"life_day_review:{lane}:{persona_id}:{target_date}"
    try:
        async with single_flight(lock_key, ttl=DAY_REVIEW_LOCK_TTL_SECONDS):
            # 硬超时包整段（证据收集 + run + 落页核验）：任何一步挂死（模型不
            # 返回 / 库查询卡死）都在锁 TTL 之前被掐掉（TimeoutError 走下面的
            # 既有 fail-open），绝不留一个挂死的班占着锁直到 TTL 被新班并发。
            await asyncio.wait_for(
                _run_day_review(
                    lane=lane,
                    persona_id=persona_id,
                    target_date=target_date,
                    now=now,
                    trace_session_id=trace_session_id,
                    trigger=trigger,
                ),
                timeout=DAY_REVIEW_TIMEOUT_SECONDS,
            )
    except SingleFlightConflict:
        # 快班与补班撞车：另一班正在回顾，静默让位（它成功会写页；它失败不写，
        # 对账班照页缺失重试）。
        logger.info(
            "[day_review] %s/%s %s another shift in flight, yield",
            lane,
            persona_id,
            target_date,
        )
    except Exception:
        logger.error(
            "[day_review] %s/%s %s 睡前回顾失败，fail-open：页不落、"
            "对账班按页缺失自动补跑",
            lane,
            persona_id,
            target_date,
            exc_info=True,
        )


async def _run_day_review(
    *,
    lane: str,
    persona_id: str,
    target_date: str,
    now: datetime,
    trace_session_id: str | None,
    trigger: Literal["sleep", "sweep"],
) -> None:
    """锁内的一次回顾编排：对账班页存在性复查 → 取证据 → 无会话 run → 记成本 → 落页核验 → 落 marker。"""
    # 对账班锁内权威复查（调用方的预检查只是省一次锁）：「那天回顾过没有」看
    # data_day_page 该 (lane, persona, target_date) 的页是否存在——绝不比对
    # LifeState.day_reviewed_date（单字段 marker 会被清晨回笼觉的快班推前到新
    # 生活日，误导对账班重跑出重复页，2026-06-12 prod 事故根因）。快班无闸：
    # 每次入睡都回顾当前生活日，新版整篇盖旧版（版本链留痕、读侧取最新版）。
    if trigger == "sweep" and await day_page_exists(
        lane=lane, persona_id=persona_id, date=target_date
    ):
        logger.info(
            "[day_review] %s/%s %s already has a day page, sweep skip",
            lane,
            persona_id,
            target_date,
        )
        return

    # 证据窗口：[生活日 04:00, 触发时刻]，意识流取 target 与 target+1 两个自然日
    # 的 session（窗口跨自然日的合同见 living_day 模块）。
    start, end = evidence_window(target_date, now)
    day_sessions: list[tuple[str, list[Message]]] = []
    for date in window_session_dates(target_date):
        history = await load_session(make_session_id(lane, persona_id, date))
        day_sessions.append((date, history))

    acts = await list_persona_acts_between(
        lane=lane,
        persona_id=persona_id,
        start_iso=start.isoformat(),
        end_iso=end.isoformat(),
    )
    chats = await find_persona_spoken_chats_in_window(
        persona_id=persona_id,
        since_ms=int(start.timestamp() * 1000),
        until_ms=int(end.timestamp() * 1000),
        per_chat_limit=PER_CHAT_MESSAGE_LIMIT,
    )

    # 空证据护栏（机制安全阀，同空信箱 early-return）：意识流 / act / 聊天全空 =
    # 这一天没有可回看的经历，不烧模型、不落 marker（marker 缺席无害：快班的
    # 前提是她活过一轮、主班对每个 target 只对账一次）。
    has_transcript = any(history for _date, history in day_sessions)
    if not has_transcript and not acts and not chats:
        logger.info(
            "[day_review] %s/%s %s no evidence at all, skip (nothing to review)",
            lane,
            persona_id,
            target_date,
        )
        return

    partner_ids = _chat_partner_ids(chats)
    old_pages = await read_relationship_pages(
        lane=lane, persona_id=persona_id, other_user_ids=partner_ids
    )
    existing_day_page = await read_day_page(
        lane=lane, persona_id=persona_id, date=target_date
    )

    pc = await load_persona(persona_id)
    messages = _review_messages(
        now=now,
        target_date=target_date,
        day_sessions=day_sessions,
        acts=acts,
        chats=chats,
        persona_id=persona_id,
        old_pages=old_pages,
        existing_day_page=existing_day_page,
    )
    # ambient 三绑定：update_day_page / update_relationship_page 从 context
    # features 读 lane / persona / target_date（缺一个工具就 LookupError 失败快）。
    # session_id 只做 langfuse 归组标签——**不**传给 run（无会话）。
    context = AgentContext(
        persona_id=persona_id,
        session_id=trace_session_id,
        features={
            FEATURE_REVIEW_LANE: lane,
            FEATURE_REVIEW_PERSONA: persona_id,
            FEATURE_REVIEW_TARGET_DATE: target_date,
        },
    )
    # max_retries=1：页写入是 durable 写，整轮重放会重放它们；中途失败直接抛 →
    # 外层 fail-open（marker 不落、下一班重试）。
    with collect_usage() as usage:
        await Agent(_REVIEW_CFG, tools=LIFE_REVIEW_TOOLS).run(
            messages,
            prompt_vars={
                "persona_name": pc.display_name,
                "persona_lite": pc.persona_lite,
            },
            context=context,
            max_retries=1,
        )

    # 成本无论成败都记（token 真烧了）：先于落页核验，"返回了但没写页"的失败班
    # 也要记账。round_id 从 (lane, persona, target_date, 触发时刻) 派生：同一天
    # 多次合法回顾（入睡快班 / 回笼觉快班 / 对账补班）各落各的账，不被幂等去重
    # 吞掉（事故里补班那次成本被去重吞的修复）；同参同刻派生确定、不漂移。
    round_id = uuid.uuid5(
        uuid.NAMESPACE_OID,
        f"{lane}\x1f{_ROUND_SEED}\x1f{persona_id}\x1f{target_date}"
        f"\x1f{now.isoformat()}",
    ).hex
    await record_round_cost(
        lane=lane,
        actor=f"{persona_id}:day_review",
        round_id=round_id,
        usage=usage,
        observed_at=now.isoformat(),
    )

    # 落标核验：run 正常返回 ≠ 回顾成功——模型可能一个工具都没调。现读昨天页：
    # 页存在（本次写的、或更早班写的——同日重跑场景算）才说明这个生活日真有了
    # 页、才落 marker（marker 已降级为观测留痕，不再当闸读——对账口径看页）；
    # 页不存在 = 本次回顾失败，不落标、error 留痕、fail-open（对账班按页缺失
    # 自动补）。关系页不核验：没聊过天就不动关系页是合法的。
    written = await read_day_page(lane=lane, persona_id=persona_id, date=target_date)
    if written is None:
        logger.error(
            "[day_review] %s/%s %s run returned but no day page was written, "
            "treat as failure: 页不落、对账班按页缺失自动补跑",
            lane,
            persona_id,
            target_date,
        )
        return
    await mark_day_reviewed(lane=lane, persona_id=persona_id, date=target_date)
    logger.info(
        "[day_review] %s/%s reviewed living day %s", lane, persona_id, target_date
    )
