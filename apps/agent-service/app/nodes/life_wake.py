"""life_wake_node — 三姐妹同构的 life 节点 (Task 3, agent 工具循环).

一套逻辑跑 akao / chinagi / ayana 三个 persona——不是三份拷贝。她是谁由唤醒她的
``EventArrived.persona_id`` 决定。

被 ``EventArrived`` 攒批唤醒后，她跑一个 **ReAct 工具循环**（不再填一张表）：

  1. 读自己的 ``LifeState``（主观快照）+ 读自己信箱里的未读 event。
  2. 跑 ``Agent(...).run`` —— 在循环里连续调工具行动：``update_life_state``
     更新此刻状态（0/N 次，多次以最后一次为准），``act`` 自主做一件影响外部世界
     的事、汇给 world 推演。她想啥、做啥、什么情绪、要不要做事，全由模型在循环里
     自己定（act 是"她做了"，不是申请待批准）。
  3. 收口：标已读 —— **只标本轮实际读到的那批 event_id**（即使一次 update 都没
     调也照常标已读：她看了但没改状态，正常）。

钉死的几条（spec / 宪法）：

  * **信息差命门**：一轮的输入 = 她自己的 ``LifeState`` + 她自己信箱的未读 event。
    本模块**绝不 import / 读 WorldState 全局快照**——全局真相一旦漏进她上下文，
    她就全知了，信息差崩塌。结构上保证：这里没有任何读 world 快照的代码。

  * **空信箱 early-return**：信箱没未读就不烧模型、不建工具、不写、不标已读。

  * **single_flight 锁**：一轮思考几十秒 > debounce 窗口，期间来新 event 会 fire
    第二轮并发；开头按 ``(lane, persona)`` 拿单飞锁，拿不到就 raise
    ``DebounceReschedule`` 交给 handler 重排（这批 event 不被吞）。

  * **失败不整轮重放**：``run`` 把整个 ReAct 循环包在 retry 里，一次 model 调用
    瞬时失败会整轮重放、重放已执行的 durable 工具（重复写快照 / 重复做事）。
    所以 life 调 ``run`` 传 ``max_retries=1`` 关掉整轮重放；中途失败就抛、本轮
    不收口（event 没标已读 → 下轮仍未读、靠 world renotify 再唤醒）。

  * **无 state_end_at、不自排闹钟**：她脑子里没有"做到几点"，只有"此刻什么样"。
    她被 event 推、**不 emit_delayed / emit_at 给自己定时唤醒**。

  * **赤尾设计宪法**：她想啥、做啥、什么情绪、要不要做事，全由模型在循环里
    判断。本模块不用阈值 / 计数器 / 随机池 / if 分支替她决策——只做 IO 编排 +
    机制安全阀（单飞锁、空信箱、inbox 上限）。

act_id 从 ``(lane, persona, 本轮读到的 event_ids)`` 派生（durable 边重投 / 重试
同一批唤醒产同一个 act_id，world 按 act_id 幂等消化），在本节点算好后
capture 进 ``build_life_tools`` 的闭包，不让模型生成。

wiring 见 ``app/wiring/life_dataflow.py``，本模块只提供 ``@node`` 函数 + 依赖。
"""

from __future__ import annotations

import logging
import uuid

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.session import load_session
from app.agent.trace import make_session_id
from app.data.queries.mailbox import list_unread_events, mark_events_read
from app.domain.life_state import find_life_state
from app.domain.world_events import EventArrived, EventEnvelope
from app.infra import cst_time
from app.infra.redis import get_redis
from app.memory._persona import load_persona
from app.nodes.life_tools import build_life_tools
from app.runtime import node
from app.runtime.debounce import DebounceReschedule
from app.runtime.single_flight import SingleFlightConflict, single_flight

logger = logging.getLogger(__name__)

# life 单飞锁的 TTL：比一轮 life 思考的最坏耗时更大的上界（LLM 几十秒级 + 工具
# 循环多轮）。锁只是基建并发控制（不替 agent 决策、不违反赤尾宪法），TTL 到期后
# 哪怕原 holder 还在跑、新 holder 也能进，token-CAS 释放保证不误删别人的锁。
_LIFE_WAKE_LOCK_TTL_SECONDS = 600

# 一轮读 inbox 的上限（spec 决策 4 安全阀）：正常够不着；积压过多时只读这批喂给
# 模型 + 只标这批已读，剩下的留未读、下轮再处理（不静默吞）。触顶要 log。
_LIFE_INBOX_MAX = 50

# 一轮跑完后的冷却时长（spec 决策 5 第三层降频）。一轮成功收口后落一个 cd key
# （TTL=这么多秒），cd 内再被唤醒就 raise DebounceReschedule 把这批 event 推迟到
# cd 后——延迟 + 合并、绝不 drop（reschedule 攒着，cd 结束一并醒）。
#
# 时长定 45s：略小于 world 的 60s 唤醒合并闸（WORLD_ACT_WAKE_DEBOUNCE_SECONDS）。
# world 是唯一启动源、被唤醒最小间隔 1min，三姐妹的轮次节奏比世界唤醒间隔密一点点
# （她们仍能在世界推进的间隙感知、回应），但已足够把"几乎每轮做事→唤醒 world→
# world 广播→三人又醒"的 82/min 量级自激压下去：一个 persona 两轮之间至少隔 45s
# + 一轮自身耗时，三人合起来最坏几轮/分钟，而非几十。这是机制层的节奏闸（跟现有
# debounce 窗口、world 60s 闸同类），不进世界内容决策（赤尾宪法）。
_LIFE_CD_SECONDS = 45

# cd key 在 redis 与 single-flight 锁分开（锁管"正在跑"，cd 管"刚跑完的冷却"），
# 用不同 key 前缀，两者不互相干扰。
def _cd_key(lane: str, persona_id: str) -> str:
    return f"life_cd:{lane}:{persona_id}"

# offline-model：异步后台思考用离线模型（见 feedback_model_selection），主对话才用
# gemini。recursion_limit 给够（让她在一轮里连续调多次工具，不被默认 6 卡住）。
# trace_name 让这一轮 life 思考接进 langfuse。
_LIFE_WAKE_CFG = AgentConfig(
    "life_wake", "offline-model", "life-wake", recursion_limit=12
)

def _format_unread(unread: list[EventEnvelope]) -> str:
    """把未读 event 拼成她"刚感知到 / 想起的几件事"的文字，按发生时间顺序。

    只放 event 的客观可感形态（summary）+ 类型 + 发生时间——这些都是投进她信箱的、
    她够得着的信息，不含任何 world 全局视角。event 的 ``occurred_at`` 在信箱里混着
    历史格式（chat 写 Unix 毫秒、world 写 CST、life 写 UTC），显示时一律过
    ``cst_time`` 归一到 CST，让她看到的所有时刻是同一个 CST 口径。
    """
    return "\n".join(
        f"- [{ev.kind}] {cst_time.to_cst_hms(ev.occurred_at)} {ev.summary}"
        for ev in unread
    )


@node
async def life_wake_node(arrived: EventArrived) -> None:
    """某姐妹被攒批唤醒，跑一轮 life 工具循环。persona 由 ``arrived`` 决定。

    **单飞命门**：一轮 life 跑几十秒 > debounce 窗口（5s），期间来新 event 会 fire
    第二轮 ``life_wake_node`` 并发。两轮并发会互相覆盖 LifeState、把 event 静默标
    已读丢掉。所以开头按 ``(lane, persona)`` 拿一把单飞锁；拿不到锁就 ``raise
    DebounceReschedule``，交给 debounce handler CAS 重排、稍后再试（这一批 event
    不被吞掉）。锁是基建并发控制、不替 agent 决策，不违反赤尾宪法。
    """
    lane = arrived.lane
    persona_id = arrived.persona_id

    lock_key = f"life_wake:{lane}:{persona_id}"
    try:
        async with single_flight(lock_key, ttl=_LIFE_WAKE_LOCK_TTL_SECONDS):
            await _run_life_round(arrived, lane=lane, persona_id=persona_id)
    except SingleFlightConflict:
        # 同 (lane,persona) 已有一轮在跑：不并发跑、不写快照、不标已读。交回
        # debounce handler 重排这一批 EventArrived，等当前那轮跑完后再醒一次。
        logger.info(
            "[life_wake] %s/%s another round in flight, reschedule", lane, persona_id
        )
        raise DebounceReschedule(arrived) from None


async def _run_life_round(arrived: EventArrived, *, lane: str, persona_id: str) -> None:
    """一轮 life 的实际编排（已在单飞锁内）：cd 检查 → 读未读 → 冷启探测 → 跑工具循环 → 收口标已读。

    **cd 降频（spec 决策 5 第三层）**：开头查 cd key——若上一轮刚跑完、还在 cd 内，
    就 ``raise DebounceReschedule(arrived)`` 把这批 event 推迟到 cd 后（延迟 + 合并、
    绝不 drop：reschedule 把 EventArrived 攒着，cd 结束一并醒一并感知）。cd 内不烧
    模型、不写、不标已读。cd 检查在单飞锁内、读 inbox 之前：single_flight 管"正在
    跑"、cd 管"刚跑完的冷却"，两层各管各的、用不同 redis key、不冲突。

    一轮成功收口（标完已读）后落一个 cd key（TTL=cd 秒）开启下一段冷却。
    """
    redis = await get_redis()
    cd_key = _cd_key(lane, persona_id)
    if await redis.get(cd_key):
        # 还在上一轮的 cd 内：把这批 event 推迟到 cd 后，绝不 drop（reschedule 攒着）。
        logger.info(
            "[life_wake] %s/%s still in cd, reschedule (events kept, not dropped)",
            lane,
            persona_id,
        )
        raise DebounceReschedule(arrived)

    unread = await list_unread_events(lane=lane, persona_id=persona_id)
    if not unread:
        # 空唤醒（去重命中后的残留信号等）：不烧模型、不建工具、不写、不标已读。
        logger.info("[life_wake] %s/%s woke with empty inbox, skip", lane, persona_id)
        return

    # 安全阀：一轮 inbox 上限。积压超限只读这批 + 只标这批已读，剩下留未读、下轮
    # 再处理（不静默吞）。触顶要 log（不静默截断）。
    if len(unread) > _LIFE_INBOX_MAX:
        logger.warning(
            "[life_wake] %s/%s inbox backlog %d > cap %d, processing first %d this "
            "round; the rest stay unread for next round",
            lane, persona_id, len(unread), _LIFE_INBOX_MAX, _LIFE_INBOX_MAX,
        )
        unread = unread[:_LIFE_INBOX_MAX]

    snapshot = await find_life_state(lane=lane, persona_id=persona_id)
    pc = await load_persona(persona_id)

    now = cst_time.now_cst()
    observed_at = cst_time.now_cst_iso()

    # system prompt 收敛成纯静态身份（spec 决策 4）：每轮都变的动态值（几点 / 上一刻
    # 状态）全出 prompt_vars——它们进 system 会让前缀缓存每轮失效、且把"会变的东西"
    # 钉死在本该恒定的身份层是语义错位。动态值改走当轮 USER message（见下方 stimulus）。
    prompt_vars = {
        "persona_name": pc.display_name,
        "persona_lite": pc.persona_lite,
    }

    # act_id 从 (lane, persona, 本轮读到的 event_ids) 派生 —— durable 边重投 /
    # 重试同一批唤醒时产同一个 act_id，world 按 act_id 幂等消化。capture
    # 进工具闭包，不让模型生成。
    read_ids = [ev.event_id for ev in unread]
    seed = f"{lane}:{persona_id}:" + ",".join(sorted(read_ids))
    act_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

    tools = build_life_tools(
        lane=lane,
        persona_id=persona_id,
        act_id=act_id,
        observed_at=observed_at,
    )

    # session 按 (lane, persona, 今天) 派生：她当天所有唤醒的 LLM 调用归进同一条
    # langfuse session，连续看一个角色的"意识流"。
    session_id = make_session_id(lane, persona_id, now.strftime("%Y-%m-%d"))
    context = AgentContext(persona_id=persona_id, session_id=session_id)

    # 冷启探测（spec 决策 5 + 双读一致性）：自己 load_session 探这条 transcript 空不空，
    # 复用 world 的"节点自己 load 一次"模式（_run_world_round 也先 load_session 做 turn
    # 幂等查重）。两次读（这里探 + Agent.run 内部续接）一致靠 (lane, persona)
    # single_flight 锁覆盖整段"探测 → run → 写回"——同 session 无并发写。探测用的就是
    # run 续接那条 session_id，绝不另起一条。
    history = await load_session(session_id)
    cold_start = not history
    if cold_start:
        # 冷启探测命中（spec 决策 5 + codex T3 可观测性）：transcript 空 → 这一轮会从
        # PG LifeState 兜底恢复状态（若有）。log 出来便于 coe 观察恢复段是否异常频繁——
        # 正常当天多轮续接不该冷启，只有首轮 / Redis 过期丢失 / 跨天新 session 才冷启。
        logger.info(
            "[life_wake] %s/%s cold start (empty transcript), %s",
            lane,
            persona_id,
            "recover from LifeState" if snapshot else "no prior state",
        )

    # 本轮她感知到的当下动静拼进 **USER stimulus**（不再走 prompt_vars→system prompt）：
    # core 的 transcript 只存"本轮传入 messages + 助手 + 工具结果"，system prompt 不进
    # transcript。若动态值留在 prompt_vars 里渲染进 system prompt，它就不进写回 session
    # 的内容、第二轮 replay 看不到"上一轮我几点、感知了什么"——她只记得自己说过做过啥、
    # 却忘了当时为何而动。把当轮感知放进 USER message（像 world 把客观 context 拼进 USER
    # 那样），它就进 messages → 进 transcript → 第二轮可 replay，"她真的记得自己经历过
    # 什么"。
    #
    # 信息差命门不破：进 transcript 的只是当下时刻 + 她自己信箱里的未读 event
    # （_format_unread 只取 summary/kind/时间，全是投给她的、她够得着的），绝不含 world
    # 全局快照。
    parts = [f"现在是 {cst_time.to_cst_hm(observed_at)}。"]

    # 状态恢复段（spec 决策 5 核心）：上一刻状态正常靠当天连续意识流（transcript）延续，
    # 不每轮重塞。只有意识流断了（冷启 / Redis 24h 过期丢失 / 跨天新 session → transcript
    # 空）时，才从 PG 的 LifeState 兜底恢复，作"醒来记得之前在做什么"喂进当前 USER。
    # 只判 transcript 空不空、不判 observed_at 是哪天（bezhai 决策：跨天先记得、不翻篇）。
    # snapshot 为 None（从没活过一轮）就不加恢复段——没有可恢复的状态，硬塞假状态反而误导。
    if cold_start and snapshot is not None:
        parts.append(
            f"你上次记得自己在做：{snapshot.current_state}"
            f"（心情 {snapshot.response_mood}、活动 {snapshot.activity_type}）。"
        )

    parts.append(
        "这会儿你感知到信箱里这些客观动静（按发生先后）：\n"
        f"{_format_unread(unread)}\n\n"
        "过你自己的这一刻。"
    )
    stimulus = "\n".join(parts)

    # max_retries=1：关掉整轮重放。run 把整个 ReAct 循环包在 retry 里，一次 model
    # 调用瞬时失败会整轮重放、重放已执行的 durable 工具（重复写快照 / 重复做事）。
    # 关掉后中途失败就抛、本轮不收口（event 没标已读 → 下轮仍未读、靠 world
    # renotify 再唤醒）。
    #
    # **显式传 session_id 续接**（spec 决策 1/3）：task1 的 run 见到显式 session_id
    # 才从 Redis 读这条 transcript 拼到 messages 前、跑完把本轮（含工具调用与结果）
    # 追加写回、刷 24h TTL（只塞 context.session_id 不读写历史）。显式 session_id
    # 优先于 context.session_id。于是三姐妹每轮接着上一轮往下、记得刚才想过做过啥。
    await Agent(_LIFE_WAKE_CFG, tools=tools).run(
        messages=[Message(role=Role.USER, content=stimulus)],
        prompt_vars=prompt_vars,
        context=context,
        session_id=session_id,
        max_retries=1,
    )

    # 收口：标已读，只标本轮实际读到的那批 event_id（绝不按 persona 全标）。即使
    # 一次 update 都没调也照常标已读——她看了但没改状态，正常。
    await mark_events_read(lane=lane, persona_id=persona_id, event_ids=read_ids)

    # cd 降频（spec 决策 5 第三层）：成功收口后开启一段冷却。落一个带 TTL 的 cd key，
    # cd 内再被唤醒就 reschedule 攒着（见本函数开头）。只在成功跑完才落——撞锁 /
    # 中途失败的轮不落，避免用虚假 cd 卡住真正该跑的下一轮。
    redis = await get_redis()
    await redis.set(_cd_key(lane, persona_id), "1", ex=_LIFE_CD_SECONDS)

    logger.info(
        "[life_wake] %s/%s ran a round, marked %d read, cd %ds",
        lane, persona_id, len(read_ids), _LIFE_CD_SECONDS,
    )
