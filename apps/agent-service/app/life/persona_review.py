"""persona review 本体 — 周级慢钟：她的经历慢慢长进「她是谁」.

照睡前回顾（:func:`app.life.review.run_day_review`）同构的无会话 agent，但钟更慢
（周级）、视角不同：睡前回顾是**她自己**回看一天，persona review 是**她的传记
作者**回读一段日子（自上次慢漂以来的日页 + 当前全部关系页 + 世界阶段 + 她现在
的身份正文），把真实留下的痕迹轻轻写进身份正文，落 persona 版本链一版
（source='review'，即刻进读路径——`_persona.py` 链最新版优先）。

机制层硬约束（纪律在 prompt 层，这里只有机制护栏——赤尾宪法）：

  * **single_flight 包整段**（key 含 lane / persona）：每日补班撞车时冲突方静默
    让位；锁内先做周级幂等复查（:func:`has_review_version_this_week`，只认
    source='review'——owner 盖版不挡班），本周已有 review 版早退。
  * **seed 在同锁内先行**（:func:`seed_persona_chain`，CAS 幂等）：首跑先把
    ``bot_persona.persona_lite`` 原文落 v0（source='seed'），agent 再写 v1。
    只落 v0 后 agent 失败＝review 未完成（幂等只认 review），下一班续补 v1
    （spec 决策 2）。
  * **证据游标 = 上一条 review 版本的 written_at**（:func:`read_latest_review_
    written_at`，owner 不动游标）：窗口取 written_at 晚于游标的全部日页（首跑
    游标 None = 全部现存页；被对账班重写过的页重新入窗，整篇重写语义下重新消化
    是对的——spec 决策 4）。条目数量控制、绝不字符截断。
  * **空窗口护栏**（机制安全阀，同空信箱 early-return）：游标之后没有任何新日页
    = 这段日子没有新经历，不烧模型、不落版——下一班（下周有新页时）自然补。
  * **fail-open**：任何一步抛错只记 error 日志、绝不向上抛——本周版不落、明天的
    补班自动重试。
  * **max_retries=1**：update_persona 是 durable 写，整轮重放会重放它。工具故意
    不包 @tool_error（写库失败穿透炸 run，见 persona_review_tools 模块 docstring）。
  * **核验**：run 正常返回 ≠ 成功——模型可能一个工具都没调。run 后复查本周
    review 版本真落了才算成功；没落 = 失败，error 留痕、下一班补。成本无论成败
    都记（token 真烧了）：actor = ``{persona}:persona_review``，round_id 从
    (lane, persona, 触发时刻) 派生——同刻确定不漂移、不同班各自入账。
  * **整段硬超时**（:data:`PERSONA_REVIEW_TIMEOUT_SECONDS`，< 单飞锁 TTL）：证据
    收集 / run / 核验任何一步挂死都被掐掉，走既有 fail-open。

改不改、改哪一笔由她的传记作者自己判断（写作纪律在 prompt 层：instruction 钉
姿态、langfuse system prompt 载全文纪律）；这里没有内容检测器（赤尾宪法）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.trace import collect_usage, make_session_id
from app.data.queries import find_persona  # module-level so tests can monkeypatch
from app.domain.thinking_cost import (  # module-level so tests can monkeypatch
    record_round_cost,
)
from app.domain.world_events import is_npc_source, strip_npc_prefix
from app.infra.cst_time import CST
from app.life.pages import (  # module-level so tests can monkeypatch
    DayPage,
    RelationshipPage,
    list_relationship_pages,
    read_day_pages_written_after,
)
from app.life.persona_chain import (  # module-level so tests can monkeypatch
    has_review_version_this_week,
    read_latest_persona_version,
    read_latest_review_written_at,
    seed_persona_chain,
)
from app.life.persona_diff_push import (  # module-level so tests can monkeypatch
    push_persona_diff,
)
from app.life.persona_review_tools import (
    FEATURE_PERSONA_REVIEW_LANE,
    FEATURE_PERSONA_REVIEW_PERSONA,
    PERSONA_REVIEW_TOOLS,
)
from app.runtime.single_flight import SingleFlightConflict, single_flight
from app.world.arc import read_world_arc  # module-level so tests can monkeypatch

logger = logging.getLogger(__name__)

# review 的独立 AgentConfig：prompt id 钉为 "persona_review"（langfuse 上由主会话
# 发布、prompt_vars 契约 = {persona_name, current_persona}）。prompt 缺失 = review
# 失败 = fail-open（下一班重试），与睡前回顾同语义。工具循环只有一件工具、一次
# 调用，recursion_limit 留小幅余量即可。
_PERSONA_REVIEW_CFG = AgentConfig(
    "persona_review", "offline-model", "persona-review", recursion_limit=6
)

# review 单飞锁 TTL：> 一次 review 的最坏耗时（一次离线模型 run + 一次工具调用，
# 分钟级），同时远 < 两班间隔（24h）。TTL 语义见 app/runtime/single_flight.py。
PERSONA_REVIEW_LOCK_TTL_SECONDS = 600

# 整段 review（证据收集 + Agent.run + 核验）的硬超时。必须 < 单飞锁 TTL（600s）
# ——锁 TTL 到期后新班就能拿锁进来，挂死的旧班必须先被掐死；取 480s 留 120s
# 安全边距（同睡前回顾的取值依据）。超时走既有 fail-open：版不落、下一班重试。
PERSONA_REVIEW_TIMEOUT_SECONDS = 480

# persona review 的成本 round_id 派生命名空间种子（与 day_review / life 轮的
# round_id 空间不混）。
_ROUND_SEED = "persona_review"


def persona_review_instruction() -> str:
    """喂给 review Agent 的任务指令（代码侧是工具语义的权威来源；零剧情事实——宪法）。"""
    return (
        "又过了一段日子。下面是她这段日子写下的日页、她心里现在的关系页、她们"
        "一家所处的现实阶段。她现在的身份正文在系统提示里——那是你这次要轻轻"
        "重写的底稿。读完这些，把这段日子**真实留在她身上的痕迹**写进身份正文，"
        "用 update_persona 把重写后的全文整篇交回。\n\n"
        "怎么改：慢而小——一次只让真实经历留下一两笔痕迹，绝大部分原文原样保留；"
        "每一处改动都必须能从下面的证据里指出出处，证据里没有的事一个字都不写；"
        "这段日子没有留下值得写进身份的变化，就把原文原样交回，绝不为改而改；"
        "她的底色不让渡——经历改变的是人生阶段、关系的厚度、新的在意，不是她"
        "性格的内核；口吻与格式与原文同族，改完读起来仍是同一篇文章。\n\n"
        "每次调用 update_persona 都是整篇重写：新的一版取代旧版。写下的时刻由"
        "系统自动记，你不用管钟。"
    )


def _window_evidence(cursor: str | None) -> str:
    """回读窗口说明：首跑（全部现存页）还是自上次慢漂以来的增量。"""
    if cursor is None:
        return "（这是第一次为她回读——下面是她全部现存的日页。）"
    return f"（上一次慢漂写于 {cursor}——下面是那之后她新写下的日页。）"


def _day_pages_evidence(pages: list[DayPage]) -> str:
    """日页证据：每页带生活日 + written_at + 全文（条目级取舍、绝不字符截断）。"""
    if not pages:
        return "（这段日子她没有写下新的日页。）"
    return "\n\n".join(
        f"〔{p.date} 这一天，写于 {p.written_at}〕\n{p.narrative}" for p in pages
    )


def _relationship_page_tag(other_user_id: str) -> str:
    """关系页证据里这一页的标签：``npc:*`` 显式标注是 NPC（建议 3），真人原样。

    persona review 全量读关系页，会读到 NPC 层写下的 ``npc:名字`` 页。证据里把这类
    页显式标成 NPC（不是真人用户标识），避免身份慢漂把这个机读键当真人写进身份正文。
    NPC 判定 / 剥前缀用 :mod:`app.domain.world_events` 的单一处定义（event source
    协议层；禁止重复定义）。真人 user_id 原样、不加任何标注。
    """
    if is_npc_source(other_user_id):
        return f"{other_user_id}（NPC：{strip_npc_prefix(other_user_id)}，不是真人用户）"
    return other_user_id


def _relationship_pages_evidence(pages: list[RelationshipPage]) -> str:
    """关系页证据：她心里现在的每一页「他与我」，带对方标识 + written_at。

    ``npc:*`` 的页在标签里显式标注是 NPC（建议 3，:func:`_relationship_page_tag`）。
    """
    if not pages:
        return "（她心里还没有写下任何关系页。）"
    return "\n\n".join(
        f"〔{_relationship_page_tag(p.other_user_id)}，这页写于 {p.written_at}〕\n"
        f"{p.narrative}"
        for p in pages
    )


def _arc_evidence(narrative: str | None) -> str:
    """世界阶段证据：最新一版 arc 的公共进展；空白如实说。"""
    if not narrative or not narrative.strip():
        return "（世界阶段还没有被写下。）"
    return narrative.strip()


def _review_messages(
    *,
    now: datetime,
    cursor: str | None,
    day_pages: list[DayPage],
    rel_pages: list[RelationshipPage],
    arc_narrative: str | None,
) -> list[Message]:
    """把 review 的全部证据拼成**单条 user 消息**（无会话、一次喂全；模板零剧情事实）。"""
    user_content = (
        f"{persona_review_instruction()}\n\n"
        f"【现实此刻】{now.isoformat()}\n"
        f"【回读窗口】{_window_evidence(cursor)}\n\n"
        f"【这段日子她写下的日页】\n{_day_pages_evidence(day_pages)}\n\n"
        f"【她心里现在的关系页】\n{_relationship_pages_evidence(rel_pages)}\n\n"
        f"【她们一家所处的现实阶段】\n{_arc_evidence(arc_narrative)}\n\n"
        "读完：把这段日子真实留在她身上的痕迹轻轻写进身份正文，用 update_persona "
        "整篇交回；没有值得写进身份的变化就原样交回。"
    )
    return [Message(role=Role.USER, content=user_content)]


async def run_persona_review(*, lane: str, persona_id: str, now: datetime) -> None:
    """跑一次 persona review。**fail-open：本函数绝不向上抛**（绝不杀 cron sweep）。

    整段包 single_flight（key 含 lane / persona）：撞锁 = 另一班正在 review 同一
    persona，静默让位（info 留痕）。其余任何异常只记 error 日志：本周版不落、
    明天的补班自动重试。
    """
    lock_key = f"persona_review:{lane}:{persona_id}"
    try:
        async with single_flight(lock_key, ttl=PERSONA_REVIEW_LOCK_TTL_SECONDS):
            # 硬超时包整段（幂等复查 + seed + 证据 + run + 核验）：任何一步挂死
            # 都在锁 TTL 之前被掐掉（TimeoutError 走下面的既有 fail-open），绝不
            # 留一个挂死的班占着锁直到 TTL 被新班并发。
            await asyncio.wait_for(
                _run_persona_review(lane=lane, persona_id=persona_id, now=now),
                timeout=PERSONA_REVIEW_TIMEOUT_SECONDS,
            )
    except SingleFlightConflict:
        logger.info(
            "[persona_review] %s/%s another shift in flight, yield",
            lane,
            persona_id,
        )
    except Exception:
        logger.error(
            "[persona_review] %s/%s persona review 失败，fail-open："
            "本周版不落、明天的补班自动重试",
            lane,
            persona_id,
            exc_info=True,
        )


async def _run_persona_review(*, lane: str, persona_id: str, now: datetime) -> None:
    """锁内的一次 review 编排：周级幂等复查 → seed v0 → 证据 → 无会话 run → 记成本 → 核验。"""
    # 锁内幂等复查（cron sweep 的预检只是省一次锁）：本周（自然周一 00:00 CST 起）
    # 已有 source='review' 的版本 = 本周班已完成。只认 review——owner 盖版不挡班
    # （spec 决策 2，语义钉在 persona_chain）。
    if await has_review_version_this_week(lane=lane, persona_id=persona_id, now=now):
        logger.info(
            "[persona_review] %s/%s already reviewed this week, skip",
            lane,
            persona_id,
        )
        return

    # seed v0 在同锁内、agent run 之前先行（CAS 幂等，链非空零操作）：首跑把
    # bot_persona.persona_lite 原文落为 v0（source='seed'）。只落 v0 后 agent
    # 失败＝review 未完成（幂等只认 review），下一班续补 v1（spec 决策 2）。
    await seed_persona_chain(lane=lane, persona_id=persona_id)

    # 证据窗口：游标 = 上一条 review 版本的 written_at（owner 不动游标，首跑
    # None = 全部现存页）。被对账班重写过的页 written_at 更新会重新入窗——整篇
    # 重写语义下重新消化是对的（spec 决策 4）。
    cursor = await read_latest_review_written_at(lane=lane, persona_id=persona_id)
    day_pages = await read_day_pages_written_after(
        lane=lane, persona_id=persona_id, written_after=cursor
    )

    # 空窗口护栏（机制安全阀，同空信箱 early-return）：游标之后没有任何新日页 =
    # 这段日子没有新经历，不烧模型、不落版。周级幂等保持 False：等有新页的那天
    # 补班自然进来。
    if not day_pages:
        logger.info(
            "[persona_review] %s/%s no new day pages since cursor=%s, "
            "skip (nothing grew)",
            lane,
            persona_id,
            cursor,
        )
        return

    rel_pages = await list_relationship_pages(lane=lane, persona_id=persona_id)
    arc = await read_world_arc(lane=lane)

    persona = await find_persona(persona_id)
    if persona is None:
        # seed 刚成功说明行存在；走到这里 = 行被并发删掉，fail fast 交给 fail-open。
        raise ValueError(
            f"persona_review: bot_persona has no row for {persona_id!r}"
        )
    # 当前身份正文 = 链最新一版（seed 后链非空；防御性 fallback 主表快照）。
    latest = await read_latest_persona_version(lane=lane, persona_id=persona_id)
    current_persona = (
        latest.narrative if latest is not None else persona.persona_lite or ""
    )

    messages = _review_messages(
        now=now,
        cursor=cursor,
        day_pages=day_pages,
        rel_pages=rel_pages,
        arc_narrative=arc.narrative if arc is not None else None,
    )
    # ambient 双绑定：update_persona 从 context features 读 lane / persona（缺一个
    # 工具就 LookupError 失败快）。session_id 只做 langfuse 归组标签（她当天的
    # 意识流 session）——**不**传给 run（无会话）。
    today = now.astimezone(CST).strftime("%Y-%m-%d")
    context = AgentContext(
        persona_id=persona_id,
        session_id=make_session_id(lane, persona_id, today),
        features={
            FEATURE_PERSONA_REVIEW_LANE: lane,
            FEATURE_PERSONA_REVIEW_PERSONA: persona_id,
        },
    )
    # max_retries=1：update_persona 是 durable 写，整轮重放会重放它。run 包
    # try/except：ReAct 循环里 update_persona 落库后模型还会再跑一轮——「版本已
    # 落但后续轮抛错」时 run 抛。本班的成败只看下面的核验（本周 review 版落没
    # 落），不看 run 是否干净返回——否则版本已在、次日被周幂等挡住，成本 / diff
    # 推送永久缺失。
    run_error: Exception | None = None
    with collect_usage() as usage:
        try:
            await Agent(_PERSONA_REVIEW_CFG, tools=PERSONA_REVIEW_TOOLS).run(
                messages,
                prompt_vars={
                    "persona_name": persona.display_name,
                    "current_persona": current_persona,
                },
                context=context,
                max_retries=1,
            )
        except Exception as exc:  # 成败交给核验判，这里只暂存（绝不吞核验路径）
            run_error = exc

    # 核验不依赖 run 成败：run 正常返回 ≠ 成功（模型可能一个工具都没调）、
    # run 抛 ≠ 失败（版本可能已落）。复查本周 review 版真落了才算成功。
    landed = await has_review_version_this_week(
        lane=lane, persona_id=persona_id, now=now
    )

    if run_error is not None and not landed:
        # run 炸且版本没落 = 真失败班：照 review.py / sediment 同款语义——run 抛
        # 的班 usage 不完整不入账。原样抛给外层 fail-open：error 留痕、本周版
        # 不落、明天的补班自动重试。
        raise run_error

    # 成本无论成败都记（token 真烧了）："返回了但没落版"的失败班、"炸了但版本
    # 已落"的班都要记账——后者的 usage 是 collect_usage 累计到炸点为止的部分
    # 用量，部分好过没有。round_id 从 (lane, persona, 触发时刻) 派生：同刻确定
    # 不漂移、当天班与次日补班各自入账，不被幂等去重吞掉。
    round_id = uuid.uuid5(
        uuid.NAMESPACE_OID,
        f"{lane}\x1f{_ROUND_SEED}\x1f{persona_id}\x1f{now.isoformat()}",
    ).hex
    await record_round_cost(
        lane=lane,
        actor=f"{persona_id}:persona_review",
        round_id=round_id,
        usage=usage,
        observed_at=now.isoformat(),
    )

    if not landed:
        # run 正常返回但本周 review 版没落（模型一个工具都没调）= 本次失败：
        # error 留痕、fail-open（明天的补班按周级幂等缺失自动补）。
        logger.error(
            "[persona_review] %s/%s run returned but no review version was "
            "written this week, treat as failure: 下一班自动补",
            lane,
            persona_id,
        )
        return
    if run_error is not None:
        # 版本已落、run 在后续轮才炸：本班算成功（成本已记、diff 推送照发），
        # error 留痕让炸点可感知。
        logger.error(
            "[persona_review] %s/%s run raised after the review version "
            "landed; the shift still counts (cost recorded, diff push proceeds)",
            lane,
            persona_id,
            exc_info=run_error,
        )
    logger.info(
        "[persona_review] %s/%s persona reviewed (cursor was %s)",
        lane,
        persona_id,
        cursor,
    )

    # diff 推送（spec 决策 6 / Task 3）：核验成功之后才推。old = 本次 review 前
    # 链上最新版（run 前读到的 current_persona，首跑 = v0 文本）、new = 链上
    # 此刻最新版（agent 刚写的）。push_persona_diff 自身 fail-open 绝不抛；这里
    # 再兜一层护住「为推送重读链上最新版」的抖动——版本已落、核验已过，推送
    # 环节的任何异常绝不能把这班翻成失败（否则外层 fail-open 的 error 会误导成
    # review 没成）。
    try:
        latest_after = await read_latest_persona_version(
            lane=lane, persona_id=persona_id
        )
        if latest_after is not None:
            await push_persona_diff(
                lane=lane,
                persona_id=persona_id,
                old_narrative=current_persona,
                new_narrative=latest_after.narrative,
                version=latest_after.version,
            )
    except Exception:
        logger.error(
            "[persona_review] %s/%s diff push step failed "
            "(review version already landed, the shift still counts)",
            lane,
            persona_id,
            exc_info=True,
        )
