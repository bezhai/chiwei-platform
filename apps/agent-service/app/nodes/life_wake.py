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
from datetime import datetime
from typing import Annotated

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
from app.nodes.life_tools import build_life_tools, fire_life_self_wake
from app.runtime import node
from app.runtime.data import Data, Key
from app.runtime.debounce import DebounceReschedule
from app.runtime.emit import emit_delayed  # module-level so tests can monkeypatch
from app.runtime.single_flight import SingleFlightConflict, single_flight

logger = logging.getLogger(__name__)

# round-scoped 待办 self-wake 容器的 features key（engine 每轮新建、schedule 工具
# 跨调用写、engine 收口后读）。让一轮内多次 schedule 覆盖而非累积（唤醒风暴命门，
# 最后一次为准），对称 world 的 FEATURE_SELF_WAKE。
FEATURE_LIFE_SELF_WAKE = "life_self_wake"


class LifeWakeTick(Data):
    """life 的自排唤醒信号（阶段 1B Task 2，对称 world 的 self ``WorldTick``）。

    她调 schedule 自排下次醒后，收口 :func:`app.nodes.life_tools.fire_life_self_wake`
    ``emit_delayed`` 一条这个信号，到期经 in-process 边接回 :func:`life_self_wake_node`。
    **独立信号、绝不复用 ``EventArrived`` 通道**（spec decision 6）：她自排醒来时信箱
    往往是空的（不是被新动静叫醒、是自己排的时间到了），复用 event 通道会因空信箱
    early return、或多次自排共用空种子误去重。所以自排是独立唤醒形态。

    transient —— 只当唤醒信号（不落 pg）；双键 (lane, persona_id) 对应某个 persona。
    ``reason``（目前只有 self）+ ``target_wake_at``（被排时的目标时刻，到期判 stale，
    照搬 :class:`app.world.engine.WorldTick` 同名字段）。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    reason: str = "self"
    # reason==self 时：这条 self 唤醒被排时的目标唤醒时刻（现实 CST aware ISO）。到期时
    # 与 LifeState 当前 next_wake_at 比对判 stale —— 不一致说明已被新自排 / 外部刺激
    # 覆盖、作废（阶段 1B 到点 gate）。照搬 WorldTick.target_wake_at。
    target_wake_at: str = ""

    class Meta:
        transient = True


# 走到点 gate 的唤醒缘由：self（life 自排）。外部刺激（EventArrived 信箱敲门 / 补敲，
# 以及未来真人聊天）永远放行、不走 gate —— 能立刻打断长睡。对称 world 的 _GATED_REASONS
# （life 没有独立保底心跳，所以只有 self 走 gate）。
_GATED_REASONS = frozenset({"self"})


def _life_self_wake_gate_passes(
    tick: LifeWakeTick,
    *,
    next_wake_at: str | None,
    now: datetime,
) -> bool:
    """到点 gate（阶段 1B Task 2，照搬 world ``_self_wake_gate_passes`` 同构逻辑）。

    判这次自排唤醒此刻作不作数：

      * **外部刺激**（``reason`` 不在 :data:`_GATED_REASONS` 里）：永远放行。life 的
        EventArrived 走的是另一个节点不进这里；这条分支是对称兜底，保证未来若有别的
        非 self 缘由从这条路进来也能放行。
      * **self**：走 gate，判两件事——
          1. **到点没到**：现实 ``now`` ≥ ``next_wake_at`` 才作数（比较一律用现实
             aware 时间）。
          2. **这条唤醒还作不作数**：self 携带它被排时的目标时刻 ``tick.target_wake_at``，
             到期时与 state 当前 ``next_wake_at`` 比对，不一致说明已被新自排 / 外部刺激
             覆盖、判废（旧 self 到期不能误触发推演）。

    ``next_wake_at`` 为 None（从没自排过：只被 world notify 起头过）时 self 判废 ——
    没有合法目标可比对，且 life 没有保底心跳兜底，None 时不该有 self 来。``next_wake_at``
    脏 / 无法解析时同样判废（不该发生，写时是 aware ISO）。

    这是"让自排意愿真生效"的机制护栏，不替她决定推演内容（赤尾宪法）。
    """
    if tick.reason not in _GATED_REASONS:
        return True

    if next_wake_at is None:
        # 从没自排过：life 没有保底心跳，self 没有合法目标可比对 → 判废。
        return False

    target = cst_time.parse(next_wake_at)
    if target is None:
        # next_wake_at 脏 / 无法解析（不该发生，写时是 aware ISO）：无可信目标 → 判废。
        return False

    # 到点没到（用现实时间比）。
    if now < target:
        return False

    # self：携带的目标时刻必须 == state 当前 next_wake_at，否则这条已被覆盖（stale）。
    carried = cst_time.parse(tick.target_wake_at)
    if carried is None or carried != target:
        return False

    return True


def _derive_life_round_id(
    *,
    lane: str,
    persona_id: str,
    wake: EventArrived | LifeWakeTick,
    read_ids: list[str],
) -> str:
    """本轮确定性标识，按**唤醒源**稳定派生（act_id 靠它，spec decision 6 命门）。

    act_id 从 round_id 派生；durable 重投 / 整轮重试要落同一 round_id 才能靠
    (lane, act_id) 自然键幂等去重，所以 round_id 不能从 ``now`` 派生（重投取新时刻 →
    新 act_id → 去重失效）。按唤醒源分：

      * **event 唤醒**：用本轮读到的 event_ids（排序）派生 —— 同一批唤醒重投得同一
        round_id → 同 act_id（重放幂等不退化，对称旧 ``sorted(read_ids)`` 种子语义）。
      * **self 唤醒**：没有 event_ids（信箱往往空），用携带的 ``target_wake_at`` 派生 ——
        每个自排轮的 target 各不相同 → round_id 各轮独立稳定，不会因空种子误去重；
        同一条 self 唤醒重投时 target 不变 → round_id 稳定、仍幂等。target 缺失（不该
        发生，self 必带）时退回 reason，至少不空种子。

    Task 3 的 life round marker（turn 幂等）将复用这个 round_id。
    """
    if isinstance(wake, LifeWakeTick):
        seed = f"{lane}\x1fself\x1f{wake.target_wake_at or wake.reason}"
    else:
        seed = f"{lane}\x1fevent\x1f" + ",".join(sorted(read_ids))
    return uuid.uuid5(uuid.NAMESPACE_OID, seed).hex


# 印在 stimulus 里的本轮标记前缀（turn 幂等查重靠它，对称 world 的 ``[world-round:``）：
# 写回 transcript 后，下次同 round_id 重投能从 session 历史里查到这行 → 跳过、不重复
# 追加同一轮、不重做 durable 工具（turn 幂等）。机读用，对模型无害（只是一行元信息）。
_ROUND_MARKER_PREFIX = "[life-round:"


def _round_marker(round_id: str) -> str:
    return f"{_ROUND_MARKER_PREFIX}{round_id}]"


def _round_already_processed(history: list[Message], round_id: str) -> bool:
    """这轮（round_id）是否已在 session 历史里出现过（turn 幂等查重，对称 world）。

    同一批唤醒重投得同一 round_id（event 用 sorted event_ids、self 用 target_wake_at
    派生）；第一次 run 把带本轮标记的 USER 消息写进 transcript，重投时这里从已读到的
    历史里查这行标记，命中即已处理过 → 跳过（不再 run、不重做 durable 工具）。
    """
    marker = _round_marker(round_id)
    for m in history:
        if m.role == Role.USER and marker in m.text():
            return True
    return False


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
    """某姐妹被信箱敲门攒批唤醒，跑一轮 life 工具循环。persona 由 ``arrived`` 决定。

    这是**外部刺激**入口（信箱来新 event / 补敲未读）：永远跑、不走到点 gate（外部
    刺激能立刻打断长睡）。空信箱 early return（没新动静不用跑）。

    **单飞命门**：一轮 life 跑几十秒 > debounce 窗口（5s），期间来新 event 会 fire
    第二轮并发。两轮并发会互相覆盖 LifeState、把 event 静默标已读丢掉。所以开头按
    ``(lane, persona)`` 拿一把单飞锁；拿不到锁就 ``raise DebounceReschedule``，交给
    debounce handler CAS 重排、稍后再试（这一批 event 不被吞掉）。锁是基建并发控制、
    不替 agent 决策，不违反赤尾宪法。
    """
    lane = arrived.lane
    persona_id = arrived.persona_id

    lock_key = f"life_wake:{lane}:{persona_id}"
    try:
        async with single_flight(lock_key, ttl=_LIFE_WAKE_LOCK_TTL_SECONDS):
            await _run_life_round(
                arrived,
                lane=lane,
                persona_id=persona_id,
                wake_kind="event",
            )
    except SingleFlightConflict:
        # 同 (lane,persona) 已有一轮在跑：不并发跑、不写快照、不标已读。交回
        # debounce handler 重排这一批 EventArrived，等当前那轮跑完后再醒一次。
        logger.info(
            "[life_wake] %s/%s another round in flight, reschedule", lane, persona_id
        )
        raise DebounceReschedule(arrived) from None


@node
async def life_self_wake_node(tick: LifeWakeTick) -> None:
    """某姐妹**自排**的时间到了，跑一轮 life 工具循环（阶段 1B Task 2）。

    这是**自排**入口（独立信号 LifeWakeTick，绝不复用 EventArrived 通道）：走到点
    gate —— 未到 next_wake_at、或携带目标被 state 当前值覆盖（stale）一律判废早返。
    放行后**即使信箱空也跑一轮**（输入语义是"你自排的时间到了，过这一刻"）。

    单飞 / cd 与 event 入口同一套锁（同 lock_key / cd_key 按 (lane, persona)）：自排
    轮和信箱轮串行化、共享冷却，不并发覆盖。撞锁吞掉（self 是她自己排的冗余唤醒，
    丢这一次无害，下次自排 / world notify 再来），不像 act 那样必须重排。
    """
    lane = tick.lane
    persona_id = tick.persona_id

    lock_key = f"life_wake:{lane}:{persona_id}"
    try:
        async with single_flight(lock_key, ttl=_LIFE_WAKE_LOCK_TTL_SECONDS):
            await _run_life_round(
                tick,
                lane=lane,
                persona_id=persona_id,
                wake_kind="self",
            )
    except SingleFlightConflict:
        # 同 (lane,persona) 已有一轮在跑：self 是冗余自排唤醒，吞掉（log 留痕、不抛）。
        # 正在跑的那轮收口时会重排自己的下次醒，丢这一次无害。
        logger.info(
            "[life_self_wake] %s/%s another round in flight, drop (redundant self wake)",
            lane,
            persona_id,
        )
        return


async def _run_life_round(
    wake: EventArrived | LifeWakeTick,
    *,
    lane: str,
    persona_id: str,
    wake_kind: str,
) -> None:
    """一轮 life 的实际编排（已在单飞锁内）：cd 检查 → gate → 读未读 → 冷启探测 → 跑工具循环 → 收口。

    ``wake_kind``：``"event"``（信箱敲门 / 补敲，外部刺激）或 ``"self"``（自排）。两条
    路径同构跑一轮，差别在三处（spec decision 1 / 6）：

      * **到点 gate**：self 走 gate（读 LifeState.next_wake_at 判到点 + stale，未到 /
        stale 判废早返）；event 是外部刺激、不走 gate、永远跑。
      * **空信箱语义**：event 空信箱 early return（没新动静不用跑）；self 即使信箱空
        也跑一轮（"你自排的时间到了，过这一刻"）。
      * **act / 幂等种子**：见 round_id 派生 —— event 用 event_ids（重放幂等），self
        没 event_ids、用 target_wake_at 派生（每个自排轮独立稳定、不误去重）。

    **cd 降频（spec 决策 5 第三层）**：开头查 cd key——若上一轮刚跑完、还在 cd 内，就把
    这次唤醒推迟到 cd 后（延迟 + 合并、绝不 drop），但**按唤醒源用不同机制**（必改 1）：
    event 走 debounce wire、``raise DebounceReschedule(wake)``（哨兵只对 debounce wire
    有意义）；self 走普通 delayed-trigger、**绝不 raise**（哨兵会被 _runtime_trigger_consumer
    当失败 drop、自排丢失），改为 emit_delayed 重排一条携带原 target 的 LifeWakeTick 延到
    cd 剩余时间后再醒。两条都 cd 内不烧模型、不写、不标已读。

    一轮成功收口（标完已读 + 排下次醒）后落一个 cd key（TTL=cd 秒）开启下一段冷却。
    """
    is_self = wake_kind == "self"

    redis = await get_redis()
    cd_key = _cd_key(lane, persona_id)
    if await redis.get(cd_key):
        # 还在上一轮的 cd 内：把这次唤醒推迟到 cd 后，绝不 drop（攒着、不丢）。**按唤醒源
        # 分两条路（必改 1 命门）**——两者都"延到 cd 后再醒"，但用的机制不同：
        #
        #   * event（EventArrived）：走 debounce wire，raise DebounceReschedule 让 debounce
        #     handler CAS 重排这批 event —— 哨兵只对 debounce wire 有意义。
        #   * self（LifeWakeTick）：走 emit_delayed → 普通 delayed-trigger MQ source，**绝不**
        #     raise DebounceReschedule —— 这个哨兵会冒泡出 _runtime_trigger_consumer 的
        #     emit，被 process(requeue=False) 当普通失败 drop（这条自排丢失、她不再自排醒、
        #     链断）。改为重新 emit_delayed 一条携带原 target 的 LifeWakeTick，延到 cd 剩余
        #     时间后再醒：把这次自排攒到 cd 后、不丢。只重排一条（防唤醒风暴）。
        if is_self:
            assert isinstance(wake, LifeWakeTick)
            # cd 剩余毫秒（pttl）：让重排的 self 正好排到 cd 结束后再醒。pttl 在极少数
            # 竞态下可能返回 -1（无 TTL）/ -2（key 刚过期）；夹到至少 1ms，保证 emit_delayed
            # 走延迟路径而非立即 emit（立即 emit 会再次命中 cd 自激）。上限是 cd 全长。
            remaining_ms = await redis.pttl(cd_key)
            delay_ms = min(
                max(int(remaining_ms), 1) if remaining_ms and remaining_ms > 0 else 1,
                _LIFE_CD_SECONDS * 1000,
            )
            logger.info(
                "[life_wake] %s/%s self wake still in cd, re-emit delayed self wake "
                "in %dms (carry target=%s, kept not dropped)",
                lane,
                persona_id,
                delay_ms,
                wake.target_wake_at or "-",
            )
            await emit_delayed(
                LifeWakeTick(
                    lane=lane,
                    persona_id=persona_id,
                    reason="self",
                    target_wake_at=wake.target_wake_at,
                ),
                delay_ms=delay_ms,
            )
            return
        # event：debounce wire 懂 DebounceReschedule，重排这批 EventArrived。
        logger.info(
            "[life_wake] %s/%s event wake still in cd, reschedule (kept, not dropped)",
            lane,
            persona_id,
        )
        raise DebounceReschedule(wake)

    # 现实此刻时间（CST）：gate 到点判定 + 喂 prompt 都用它（gate 比较一律用现实时间）。
    now = cst_time.now_cst()

    # 到点 gate（self 自排走 gate，event 外部刺激放行）：未到 next_wake_at、或携带目标
    # 被 state 当前值覆盖（stale）一律判废、早返（不烧模型、不写、不标已读）。读
    # LifeState 拿当前 next_wake_at 判 gate；放行的 event 轮也会用到 snapshot（状态恢复），
    # 这里读一次复用。
    snapshot = await find_life_state(lane=lane, persona_id=persona_id)
    if is_self:
        next_wake_at = snapshot.next_wake_at if snapshot is not None else None
        assert isinstance(wake, LifeWakeTick)
        if not _life_self_wake_gate_passes(
            wake, next_wake_at=next_wake_at, now=now
        ):
            logger.info(
                "[life_self_wake] %s/%s self wake gated out (now=%s next_wake_at=%s "
                "carried_target=%s): not due / stale, skip",
                lane,
                persona_id,
                now.isoformat(),
                next_wake_at,
                wake.target_wake_at or "-",
            )
            return

    unread = await list_unread_events(lane=lane, persona_id=persona_id)
    if not unread and not is_self:
        # event 唤醒空信箱（去重命中后的残留信号等）：没新动静，不烧模型、不写、不标。
        # self 唤醒空信箱**不** early return —— 她自排的时间到了，照样过这一刻（decision 6）。
        logger.info("[life_wake] %s/%s event wake empty inbox, skip", lane, persona_id)
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

    # snapshot / now 已在 gate 段读过，这里直接复用（同一锁内、无并发改）。
    pc = await load_persona(persona_id)

    observed_at = now.isoformat()

    # system prompt 收敛成纯静态身份（spec 决策 4）：每轮都变的动态值（几点 / 上一刻
    # 状态）全出 prompt_vars——它们进 system 会让前缀缓存每轮失效、且把"会变的东西"
    # 钉死在本该恒定的身份层是语义错位。动态值改走当轮 USER message（见下方 stimulus）。
    prompt_vars = {
        "persona_name": pc.display_name,
        "persona_lite": pc.persona_lite,
    }

    read_ids = [ev.event_id for ev in unread]

    # 本轮确定性标识，按**唤醒源**稳定派生（Task 3 的 turn 幂等 round marker 复用它）：
    #   * event 唤醒：用本轮读到的 event_ids（排序），重投得同一 round_id（重放幂等）。
    #   * self 唤醒：没有 event_ids（信箱往往空），用携带的目标时刻 target_wake_at 派生
    #     —— 每个自排轮 target 各不相同 → round_id 各轮独立稳定，不因空种子误去重。
    round_id = _derive_life_round_id(
        lane=lane, persona_id=persona_id, wake=wake, read_ids=read_ids
    )

    # act_id 派生（spec decision 6 命门：对自排也成立的稳定派生，不依赖 event_ids）：
    #   * event 唤醒：保持原种子 (lane:persona:sorted(event_ids)) 不变 —— durable 边
    #     重投 / 重试同一批唤醒产同一 act_id，world 按 act_id 幂等消化（重放幂等语义
    #     **绝不退化**）。
    #   * self 唤醒：event_ids 为空，原种子 (lane:persona:) 多个自排轮共用 → 误去重。
    #     改用 round_id 派生 —— 每个自排轮独立稳定的种子，同一条 self 重投仍同 act_id。
    # capture 进工具闭包，不让模型生成。
    if is_self:
        act_seed = f"{lane}:{persona_id}:self:{round_id}"
    else:
        act_seed = f"{lane}:{persona_id}:" + ",".join(sorted(read_ids))
    act_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, act_seed))

    # round-scoped 待办 self-wake 容器：schedule 工具往里写 delay_ms（覆盖而非追加 →
    # 一轮内最后一次为准），engine 收口后读它 emit 至多一条 self LifeWakeTick。
    self_wake: dict = {}

    tools = build_life_tools(
        lane=lane,
        persona_id=persona_id,
        act_id=act_id,
        observed_at=observed_at,
        self_wake=self_wake,
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

    # turn 幂等（阶段 1B Task 3，对称 world 的 round marker）：load_session 读已有
    # transcript，若本轮 round_id 标记已在历史里 → 收口跳过，不再 run / 不重做 durable
    # 工具（act / 写快照）/ 不重复追加同一轮 / 不重复标已读。覆盖两个唤醒源叠加的重放：
    #   * durable 重投（整轮重试 / delayed trigger 重投）：同一条唤醒重投得同一 round_id。
    #   * debounce 补敲的 EventArrived：同一批 event → 同 read_ids → 同 round_id。
    # 单飞锁 + 45s cd 只挡"同时并发 / 短时连发"，挡不住 durable 重投跨更长时窗、或 cd
    # 过后的补敲；round marker 才是真正的 turn 幂等护栏（意识流落 PG durable 之后，缺它
    # 这个缺陷从"24h 自愈"变永久）。锁覆盖全段保证 load → run → 写回 之间 round 标记
    # 不被并发抢写。读不到（过期 / 首次）按空历史走、正常跑（冷启降级，不报错）。这是
    # 机制护栏（防重放），不替 agent 决策（赤尾宪法）。
    if _round_already_processed(history, round_id):
        logger.info(
            "[life_wake] %s/%s round %s already in transcript, skip (turn idempotent)",
            lane,
            persona_id,
            round_id,
        )
        return

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
    # 开头印一行本轮标记（round_id）：写回 transcript 后，下次同 round_id 重投能从
    # session 历史里查到这行 → turn 幂等跳过（对称 world 把标记印进 USER stimulus）。
    # 机读用，对模型无害（它只当是一行元信息）。
    parts = [_round_marker(round_id), f"现在是 {cst_time.to_cst_hm(observed_at)}。"]

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

    if unread:
        parts.append(
            "这会儿你感知到信箱里这些客观动静（按发生先后）：\n"
            f"{_format_unread(unread)}\n\n"
            "过你自己的这一刻。"
        )
    elif is_self:
        # self 唤醒且信箱空（decision 6）：不是被新动静叫醒、是自己排的时间到了。输入
        # 语义就是"你自排的时间到了，接着过下一刻"——没有外部新动静，照样往下过日子
        # （写完这题接着写下一题、收拾完挪去客厅）。她可以接着用 schedule 排下次醒、
        # 续上自排接力。
        parts.append(
            "没有新的外部动静——是你之前自己排的时间到了。接着过你自己的下一刻吧"
            "（接着做手上的事、或换个状态），需要的话再用 schedule 排下次醒。"
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
    # 一次 update 都没调也照常标已读——她看了但没改状态，正常。self 唤醒空信箱时
    # read_ids 为空，mark_events_read([]) 无副作用、安全。
    await mark_events_read(lane=lane, persona_id=persona_id, event_ids=read_ids)

    # 收口排下次醒（阶段 1B Task 2）：本轮若调过 schedule，self_wake 里有 delay_ms ——
    # fire 算目标时刻、写进 LifeState.next_wake_at、emit 至多一条 self LifeWakeTick
    # （携带目标时刻供 stale 判定）。没调过 schedule（空容器）就不 emit —— 她不自排
    # 接力时不会自己醒，靠 world 下一次 notify 起头。event / self 两条路都走这收口，
    # 所以被 world 起头唤醒一次后，她就能用 schedule 自排接力往下过日子（spec 目标）。
    await fire_life_self_wake(lane=lane, persona_id=persona_id, self_wake=self_wake)

    # cd 降频（spec 决策 5 第三层）：成功收口后开启一段冷却。落一个带 TTL 的 cd key，
    # cd 内再被唤醒就 reschedule 攒着（见本函数开头）。只在成功跑完才落——撞锁 /
    # 中途失败的轮不落，避免用虚假 cd 卡住真正该跑的下一轮。
    await redis.set(_cd_key(lane, persona_id), "1", ex=_LIFE_CD_SECONDS)

    logger.info(
        "[life_wake] %s/%s ran a %s round, marked %d read, self_wake=%s, cd %ds",
        lane, persona_id, wake_kind, len(read_ids),
        "yes" if self_wake.get("delay_ms") else "no", _LIFE_CD_SECONDS,
    )
