"""world engine 节点 — 阶段 1A（world 推演者）.

world 是这个世界的推演层，不是导演。它被**三源唤醒**，每次唤醒走同一条回路：
先对账信箱自愈 → 算现实此刻时间（CST）→ 读自己上一版客观世界叙述（无快照=冷
启动）→ 把"上一版世界叙述 / 现在几点 / 这批角色动作"作为 prompt context 一次喂
全（世界设定本身——这家是谁、各自客观作息——由 system prompt 一处承载，USER
层不再重复拼），**跑一个 agent 工具循环**：world 在循环里推演世界此刻什么样、
用 update_world 写下世界叙述、对收到的角色动作推演客观结果、用 notify 把够得着的
角色推演出来投客观动静、用 sleep 定下次再看。

它不再"填一张表"返回结构化大对象。"世界此刻什么样""谁够得着一条动静""产什么
客观动静""睡多久"全在循环里由 LLM 用工具表达——把一个被训练成"连续调工具行动"
的模型用它擅长的方式驱动，世界由它推演、不再凝固。

新范式与旧设计的根本差别：旧设计 world 是导演 / 裁决者（move_persona 替角色挪
位置、emit_event 按"谁在某 room"广播、被 intent 唤醒后裁准 / 拒绝角色意图）。
现在 world 退成推演者——它绝不替角色决定她想做什么 / 怎么想 / 什么情绪（那是角色
自己的事），它对角色动作（act）只推演"客观上发生了什么"、绝不批准 / 拒绝她想不想
做（她几乎总能做到，除非客观世界里有硬冲突）。谁够得着一条动静由 world 推演（在
厨房的人闻得到厨房的香味、在学校的够不着），不是查表。

三个唤醒源（都打到 ``world_tick`` 节点，靠 :class:`WorldTick` 的 ``reason``
区分）：

  1. **保底心跳**：``Source.interval(WORLD_HEARTBEAT_SECONDS)`` 每 10 分钟喂一条
     单字段 :class:`WorldHeartbeatTick`（满足框架时间源的单字段 ts 约定），由
     :func:`heartbeat_to_world_tick` 翻成 ``WorldTick(reason="heartbeat")`` 踹
     world 一下（世界时钟的滴答，只叫它看一眼）。这钉死最长停摆——所有 life
     都靠 world 启动，world 睡死世界就死。时间源不直接喂 ``WorldTick``：那会在
     源循环 ``_build_payload(WorldTick(ts=...))`` 处 ValidationError 杀 Pod
     （``WorldTick`` 无 ts、缺必填 lane），world 在生产里永远起不来。
  2. **自排下次醒**：world 在循环里调 ``sleep(seconds)`` 工具
     （:func:`app.world.tools.sleep`）决定下次几时醒，收口经
     :func:`app.world.tools.fire_self_wake` ``emit_delayed`` 一个
     ``WorldTick(reason="self")``，到期经 in-process 边接回本节点。sleep 上下限
     60s~1h，超限工具报错喂回模型重调，不静默夹。
  3. **life 回灌的动作**：life emit ``ActPerformed`` → :func:`act_to_world_tick`
     翻成 transient ``ActWorldTick`` 走 60s 合并闸，闸后 :func:`world_act_wake`
     翻成 ``WorldTick(reason="act")`` 打到这个节点，world 被唤醒去**推演客观结果**。

赤尾设计宪法（硬约束）：
  * "世界此刻什么样""谁够得着""产什么客观动静""睡多久"全由 LLM 在循环里用工具
    判断——代码里没有任何阈值 / 计数器 / 随机池 / if 分支替它决策。10 分钟心跳 /
    sleep 上下限 / recursion_limit 只决定"何时醒 / 别失控"，绝不进入世界内容决策。
  * world 只做"客观事实 → 客观可感叙述 / 形态"的感官投影，**绝不碰情绪 / 主观
    解读**（那是 life 的事）。这条由喂 LLM 的 :func:`world_loop_instruction` 在
    prompt 层钉死。
  * 谁够得着一条动静由 world 推演（在 prompt 层判断），不是查表——没有 presence
    表、没有同-room 机械匹配。这条由 :func:`app.world.tools.notify` 落实（recipients
    由 world 推演给出）。

失败语义命门：循环调 ``Agent.run`` 必须传 ``max_retries=1``——core 的 ``run``
把整轮 ReAct 包在 ``@retry`` 里，一次 model 调用瞬时失败会整轮重放、重放已执行
的 durable 工具（update_world / notify 是 durable 写）。关掉整轮重放后中途失败就
抛、收口本轮已做的，靠 10min 保底心跳 + 开头的 ``renotify_unread`` 下次补。

框架原语：``Source.interval`` 心跳、``emit_delayed`` 自排（经 sleep 工具）、
``deliver_event`` 投递（经 notify 工具）、``insert_append`` / ``select_latest``
快照（经 update_world 工具 / ``app.world.state``）、``Agent.run`` agent 循环。本
节点只用现成原语，不改 runtime / core。

wiring（interval 心跳源 → WorldHeartbeatTick → heartbeat_to_world_tick；
ActPerformed → act_to_world_tick；WorldTick 纯 in-process 接回 world_tick）
在 ``app/wiring/life_dataflow.py`` 收口。本模块提供 world 节点 + 唤醒信号 domain
+ agent 循环组装 + 两个翻译节点；world 的工具集在 ``app/world/tools.py``。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.session import load_session
from app.agent.trace import make_session_id
from app.data.queries.acts import list_recent_acts
from app.data.queries.mailbox import renotify_unread
from app.domain.world_events import ActPerformed
from app.infra import cst_time
from app.runtime.data import Data, Key
from app.runtime.emit import emit  # module-level so tests can monkeypatch
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node
from app.runtime.single_flight import SingleFlightConflict, single_flight
from app.world.state import read_world_state
from app.world.tools import (
    FEATURE_SELF_WAKE,
    WORLD_TOOLS,
    fire_self_wake,
)

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

# 保底心跳：world 最长不睡过 10 分钟。这钉死最长停摆——所有 life 都靠 world
# 启动，world 睡死世界就死。world 自己用 sleep 工具定下次几时醒（≤1h），这条
# interval 是"睡死了没人踹"的兜底。
WORLD_HEARTBEAT_SECONDS = 600
WORLD_HEARTBEAT_MS = WORLD_HEARTBEAT_SECONDS * 1000

# agent 循环的 recursion_limit：给 world 足够轮数连续推演（一轮 model 调用可 batch
# 多个工具，10~15 足矣）。设够而非无限，是"别失控空转"的安全阀，不进世界内容决策。
WORLD_RECURSION_LIMIT = 12

# world 串行化锁 TTL：比一轮 world 思考的最坏耗时更大的上界（LLM 几十秒 + 工具
# 循环多轮）。确定性 session_id 把三源唤醒打到同一个 transcript key，无锁并发会
# 互相覆盖；所以 world 像 life 一样按 actor（lane）串行化，锁覆盖「读历史 →
# run/工具副作用 → 写回」整段。锁只是基建并发控制（不替 agent 决策、不违赤尾
# 宪法），TTL 到期后哪怕原 holder 还在跑、新 holder 也能进，token-CAS 释放保证
# 不误删别人的锁。
WORLD_TICK_LOCK_TTL_SECONDS = 600

# act→world 合并闸窗口：「world 被唤醒最小间隔 1 分钟」。短于 1min 的连续 act
# （任意姐妹）合并成一次 world 唤醒，光靠 sleep 下限挡不住 act 立即唤醒。复用现成
# debounce 原语：闸放在闸后的 transient ActWorldTick 上（ActPerformed 是 durable
# 持久化 Data、不能直接 debounce），ActPerformed → act_to_world_tick 那条 durable
# 边原样保留、act_id 幂等不被破坏。这是机制层节奏闸（何时醒），不进内容决策。
WORLD_ACT_WAKE_DEBOUNCE_SECONDS = 60
# max_buffer：合并闸攒够这么多条立即触发一次（debounce 的 fire_now 安全阀）。设到
# 大到正常够不着的量级，让"world 被唤醒最小间隔 60s"硬闸更硬：太小会在窗口内攒够
# N 条时立即触发一次 world 唤醒、破坏 60s 闸。act 经 life 已降频、本不该高频，所以
# 闸抬到 10000——内容不再靠闸传一条 payload（latest-only 会丢前几条），而是 world
# 醒来从 PG 读全那一批（list_recent_acts），溢出风险消失，max_buffer 纯粹只剩"别让
# debounce 队列无界涨"的兜底意义。
WORLD_ACT_WAKE_MAX_BUFFER = 10000

# act 唤醒时从 PG 读最近 act 的回看窗口跨度。window 下界**锚定触发这次唤醒的 act 的
# occurred_at**（不是 now、也不是 world_time 快照），往前回看这么多秒：
#
#   since = 触发 act 的 occurred_at - WORLD_ACT_LOOKBACK_SECONDS
#
# 为什么锚定 act occurred_at 而不是 now / world_time（命门）：
#   * 合并闸 latest-only 只把**最后那条** act 透进 wake，但同一窗口的姐妹 act
#     occurred_at 更早（最多早一个 debounce 窗口 ≈ 60s）。从触发 act 往前回看
#     一个窗口就能把这批姐妹全覆盖。
#   * world_time 不是"act 已消费游标"：heartbeat / self / 并发 world 轮次会推进
#     world_time 却**不读 act**，随后 act wake 若用更晚的快照当下界，会把这条
#     未消费 act 排出窗口 → 又静默丢。所以下界绝不依赖 world_time。
#   * 触发 act 的 occurred_at 跨 durable 重投 / 撞锁 reschedule **稳定不变**（同
#     一条 act 重新投递 occurred_at 不变），所以重排延迟不会缩窗、不会漏读。
#
# 90s 略大于 60s 合并闸窗口：覆盖一个合并周期内最早那条姐妹 act，又不至于把几个
# 周期前推演过的旧 act 大批拖进来。重复读到极少量已推演旧 act 无害——world 看上一版
# 世界叙述 + 有 session 续接认得出推演过的，不会重复推演 / 重复 notify。
WORLD_ACT_LOOKBACK_SECONDS = 90

_WORLD_CFG = AgentConfig(
    "world_deliberate",
    "offline-model",
    "world-deliberate",
    recursion_limit=WORLD_RECURSION_LIMIT,
)


class WorldHeartbeatTick(Data):
    """保底心跳的时间源信号——纯"踹一下"，单字段 ``ts``。

    框架硬约定（runtime ``_build_payload``）：cron / interval 时间源每 tick 只
    用 ``data_type(ts=<iso>)`` 构造 payload，所以时间源的 Data 必须是带
    ``ts: str`` 的单字段 tick（正例 :class:`app.domain.life_dataflow.MinuteTick`）。
    心跳只决定"何时醒"、不进世界内容决策，也不需要 lane（lane 在翻译节点
    :func:`heartbeat_to_world_tick` 按进程级泳道填），所以它干净地只有 ts。
    """

    ts: Annotated[str, Key]

    class Meta:
        transient = True


class WorldTick(Data):
    """world 的唤醒信号（三源都打这个 Data 到 ``world_tick`` 节点）。

    transient——只当唤醒信号，世界内容在 durable 快照 / 信箱里。``reason``
    区分唤醒源（heartbeat / self / act）；动作回灌时透出 act_* 供 LLM 推演。

    纯 in-process：``WorldTick`` 不直接挂时间源（时间源的形态约束由
    :class:`WorldHeartbeatTick` 承载），它只承载三种 in-process 来源——心跳翻译
    （:func:`heartbeat_to_world_tick`）、``world_tick`` 自排（``emit_delayed``）、
    动作翻译（:func:`world_act_wake`）。
    """

    lane: Annotated[str, Key]
    reason: str = "heartbeat"          # heartbeat | self | act
    # reason==self 时：这条 self 唤醒被排时的目标唤醒时刻（现实 CST aware ISO）。
    # 到期时与 WorldState 当前 next_wake_at 比对判 stale —— 不一致说明这条 self 已被
    # 新自排 / 外部刺激覆盖、作废（阶段 1B 到点 gate）。Task 2 的 life 自排照搬此字段。
    target_wake_at: str = ""
    act_id: str = ""                   # reason==act 时：动作稳定标识（round_id 派生靠它）
    act_persona_id: str = ""           # reason==act 时：谁做的
    act_description: str = ""          # reason==act 时：她做了什么（自然语言）
    act_occurred_at: str = ""          # reason==act 时：做这件事的时刻（PG 读窗口锚点）

    class Meta:
        transient = True


class ActWorldTick(Data):
    """act→world 合并闸的 transient 信号（最小唤醒间隔 1min）。

    act 唤醒 world 不再直接打 ``WorldTick``，而是先翻成这条 transient
    ``ActWorldTick`` 走 60s debounce 合并闸：短于 1min 的连续 act（任意姐妹）合并
    成一次 world 唤醒，闸后的 :func:`world_act_wake` 再翻成 ``WorldTick(reason="act")``
    打到 :func:`world_tick`。

    为什么不直接 debounce ``ActPerformed``：``ActPerformed`` 是 durable 持久化
    Data（有 PG 表、自然键 ``(lane, act_id)``），而 debounce 的硬约束是
    ``Meta.transient = True`` 且不能跟 ``.durable()`` 组合。所以闸放在闸后这条干净
    的 transient 信号上，``ActPerformed → act_to_world_tick`` 的 durable 边原样
    保留——act_id 派生那套 durable 幂等不被破坏。

    ``act_*`` 字段透传：``act_id`` 既是 round_id 派生源（重投幂等命门）也是闸后
    world_tick 的本轮标识；persona / description 透给 world 循环推演。
    """

    lane: Annotated[str, Key]
    act_id: str = ""
    act_persona_id: str = ""
    act_description: str = ""
    act_occurred_at: str = ""          # 触发 act 的时刻（闸后 world 读 PG 窗口的锚点）

    class Meta:
        transient = True


def act_wake_key(wake: ActWorldTick) -> str:
    """合并闸分区键：按 lane 分区（world 是单 actor，同 lane 的 act 合并一次唤醒）。

    「world 被唤醒最小间隔 1 分钟」是 world 这个单 actor 的全局节奏，所以按 lane
    （= 一个 world）分区，而不是按 persona —— 同一 lane 里任意姐妹在 1min 窗口内的
    连续 act 都合并成一次 world 唤醒。
    """
    return f"world:{wake.lane}"


def world_loop_instruction() -> str:
    """喂给 world agent 循环的指令：world 是世界的推演层、不是导演。

    赤尾设计宪法在 prompt 层的钉子——world 是客观层，只产"客观可感形态"、只做客观
    推演，禁止情绪 / 主观解读。降频的软引导也在这里：世界大部分时刻安静流动，没有
    值得感知的客观变化就只 update_world + sleep、不 notify。这是引导她自己判断，
    不是加 if 分支强制（赤尾宪法：不用规则替 agent 决策）；配合连续记忆她也会知道
    "刚才已经够热闹了"。
    """
    return (
        "你是这个世界的推演层（world）。你不是导演、不是裁判——你不替任何角色决定"
        "她想做什么、怎么想、什么情绪（那是各角色自己的事）；你对角色做的事只推演"
        "客观上发生了什么，绝不批准或拒绝她想不想做（她几乎总能做到，除非客观世界"
        "里有硬冲突）。情绪和主观解读不是你的事。\n\n"
        "你不是填一张表，而是一个会持续推演世界的脑子。你有三个工具，看一眼世界后"
        "想清楚再调，直到这一轮没有别的要做了就停：\n\n"
        "- update_world(detail)：写下世界此刻的客观叙述。看你记得的上一版世界叙述"
        "+ 现在几点，推演世界此刻什么样：谁大概在哪、在干嘛、什么氛围（位置就融在"
        "叙述里，不用专门的房间字段）；对收到的角色动作，把它的客观结果也体现进来"
        "（她去厨房 → 厨房有了动静和她的身影）。只写客观发生了什么，绝不写谁的情绪"
        "/ 心情 / 主观解读。世界时间由系统按现实时刻自动记，你不用编。\n"
        "- notify(recipients, observation)：把一条客观动静投给你推演出此刻够得着它"
        "的角色。谁够得着由你推演——在厨房的人闻得到厨房的香味、在学校的够不着，"
        "不是查表。observation 必须是感官投影——‘厨房飘来煎蛋和咖啡的香味’‘玄关"
        "传来开关门的声音’‘晌午的光斜照进房间’。绝对禁止写情绪、心情、主观解读、"
        "建议或指令。没人够得着就别投。\n"
        "- sleep(seconds)：看完这一轮，定多久后再来看一眼世界（必须在 60～3600 秒"
        "之间，也就是最短 1 分钟、最长 1 小时）。这是你唯一的自排手段。\n\n"
        "世界大部分时刻是安静流动的，不是每次醒来都要制造点动静。先看一眼你之前"
        "记得的世界叙述——刚才已经发生过、还在持续的事（一顿饭、一节课、一段午后）"
        "不用重复广播一遍。当有人做了一件事、或给了反馈时：推演它的客观结果、把它"
        "体现进新的世界叙述（update_world），但如果这件事本就顺着世界此刻的样子、"
        "没产生一个值得别人感知的新客观动静（比如她在自己房间里翻了个身），那就只"
        "update_world + sleep，不用 notify。只有当真发生了一个值得被感知的客观变化"
        "（环境里出现了新的声响光线气味、有人进出了某个空间），才用 notify 把够得着"
        "的人推演出来、投这条动静。\n"
        "也不要为了'让世界别太安静'硬造动静——安静本身就是工作日午后真实的样子。\n\n"
        "看完、推演完这一轮后，用 sleep 定下次多久再看。"
    )


# 走到点 gate 的唤醒源：self（world 自排）与 heartbeat（保底心跳）。外部刺激
# （act 角色动作、未来真人聊天）永远放行、不走 gate——能立刻打断长睡。
_GATED_REASONS = frozenset({"self", "heartbeat"})


def _self_wake_gate_passes(
    tick: WorldTick,
    *,
    next_wake_at: str | None,
    now: datetime,
) -> bool:
    """到点 gate（阶段 1B Task 1）：判这次唤醒此刻作不作数。

    唤醒按语义分两类：

      * **外部刺激**（``reason`` 不在 :data:`_GATED_REASONS` 里，如 act 角色动作）：
        永远放行——外部刺激能立刻打断长睡。
      * **self / heartbeat**：走 gate，判两件事——
          1. **到点没到**：现实 ``now`` ≥ ``next_wake_at`` 才作数（比较一律用现实
             aware 时间，**不用 world_time**：world_time 会因 gate 停滞、拿它判到点
             会永远不醒）。
          2. **这条唤醒还作不作数（仅 self）**：self 携带它被排时的目标时刻
             ``tick.target_wake_at``，到期时与 state 当前 ``next_wake_at`` 比对，不一致
             说明已被新自排 / 外部刺激覆盖、判废（旧 self 到期不能误触发推演）。

    ``next_wake_at`` 为 None（从没排过：首轮 / 冷启 / 只 update_world 没 sleep）时
    心跳放行（别卡死首轮）；self 不该在没排过时来，None 时 self 也判废（没有合法
    目标可比对）。

    这是"让自排意愿真生效"的机制护栏，不替 world 决定推演内容（赤尾宪法）。
    """
    if tick.reason not in _GATED_REASONS:
        return True

    if next_wake_at is None:
        # 从没排过下次醒：心跳放行（首轮不卡死）；self 没有合法目标可比对、判废。
        return tick.reason == "heartbeat"

    target = cst_time.parse(next_wake_at)
    if target is None:
        # next_wake_at 脏 / 无法解析（不该发生，写时是 aware ISO）：心跳放行兜底，
        # 别因脏 state 把世界卡死；self 无可信目标可比对、判废。
        return tick.reason == "heartbeat"

    # 到点没到（用现实时间比，不用 world_time）。
    if now < target:
        return False

    # self：携带的目标时刻必须 == state 当前 next_wake_at，否则这条已被覆盖（stale）。
    if tick.reason == "self":
        carried = cst_time.parse(tick.target_wake_at)
        if carried is None or carried != target:
            return False

    return True


def _wake_reason_text(tick: WorldTick, *, cold_start: bool) -> str:
    """把唤醒信号翻成给 world 循环的缘由文本。"""
    if cold_start:
        return (
            "世界冷启动：这是 world 首次醒来，还没有上一版世界叙述。请按现实当前"
            "时间 + 你已知的这个世界（谁住在这、各自客观上大致的一天），推演此刻"
            "世界大致什么样（谁大概在哪、在干嘛、什么氛围），用 update_world 写下"
            "第一版世界叙述。"
        )
    if tick.reason == "act":
        # act 唤醒的缘由文本不再只点最后一条 —— 这一批所有人的动作由
        # _act_batch_text 从 PG 读全后呈现（见 _world_loop_messages）。这里只给
        # 一句总起，具体每条在批次清单里。
        return "有人做了些事，要你推演它们的客观结果（这一批所有人的动作见下方清单）。"
    if tick.reason == "self":
        return "上一轮你自排的提前卡点到了，再看一眼世界。"
    return "例行看一眼世界，看看此刻该推演些什么。"


def _act_since_cutoff(tick: WorldTick, now: datetime) -> str:
    """算 act 回看窗口下界：**锚定触发 act 的 occurred_at** 往前回看一个窗口。

      since = 触发 act 的 occurred_at - WORLD_ACT_LOOKBACK_SECONDS

    绝不锚 now / world_time（命门）：world_time 不是 act 已消费游标，heartbeat /
    self / 并发轮次会推进它却不读 act，用更晚的 world_time 当下界会把未消费 act 排出
    窗口又静默丢。触发 act 的 occurred_at 跨重投 / reschedule 稳定不变，从它往前回看
    一个 debounce 窗口就覆盖同批最早那条姐妹 act。

    ``tick.act_occurred_at`` 缺失 / 脏 / naive（老链路没透传、或脏数据）时退回
    ``now - lookback`` 兜底——不会比锚 occurred_at 更宽，但保证不抛、仍读得到近窗 act。
    """
    fallback = (now - timedelta(seconds=WORLD_ACT_LOOKBACK_SECONDS)).isoformat()
    raw = tick.act_occurred_at
    if not raw:
        return fallback
    try:
        anchor = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return fallback
    if anchor.tzinfo is None:
        # naive occurred_at 跟 aware 做 timedelta 安全，但与 now 语义不一致，退回兜底。
        return fallback
    return (anchor - timedelta(seconds=WORLD_ACT_LOOKBACK_SECONDS)).isoformat()


def _act_batch_text(acts: list[ActPerformed]) -> str:
    """把这段时间的所有 act 拼成一段清单文本喂给 world（对称 life 读 mailbox）。

    呈现每个人此刻做了什么，让 world 看到这一批所有人的动作、逐条推演客观结果——
    不再只剩合并闸 latest-only 透进来的最后一条（前面几条对 world 等价丢失是命门）。
    空批次给一句兜底（合并闸 fire 时 PG 可能因极端时序暂查不到）。
    """
    if not acts:
        return "（这段时间没读到具体动作记录，按你看到的世界现状推演该不该推进。）"
    # act 的 occurred_at 来自 life 历史（可能 UTC），显示时过 cst_time 归一到 CST——
    # 跟 world_time（CST）同框、模型看到的所有时刻是同一个 CST 口径。
    lines = [
        f"- {a.persona_id or '某人'}：{a.description}（{cst_time.to_cst_hms(a.occurred_at)}）"
        for a in acts
    ]
    return "\n".join(lines)


# 印在 stimulus 里的本轮标记前缀（turn 幂等查重靠它）：写回 transcript 后，下次
# 同 round_id 重投能从 session 历史里查到这行 → 跳过、不重复追加同一轮、不重复
# 推演（turn 幂等）。机读用，对模型无害（它只当是一行元信息）。
_ROUND_MARKER_PREFIX = "[world-round:"


def _round_marker(round_id: str) -> str:
    return f"{_ROUND_MARKER_PREFIX}{round_id}]"


def _world_loop_messages(
    *,
    detail: str,
    now_iso: str,
    wake_reason: str,
    round_id: str,
    act_batch_text: str = "",
) -> list[Message]:
    """把"上一版世界叙述 / 现在几点 / 这批动作"拼成喂给循环的 user 消息。

    ``detail`` 是上一版世界叙述（冷启动时是一句"首次醒来、还没有上一版世界叙述"的
    占位文本）。``now_iso`` 是 engine 算的现实此刻时间（CST）。这里把当前客观 context
    一次喂全，让 world 在循环里推演 + 用 update_world 写新的世界叙述。开头印一行本轮
    标记（``round_id``），写回 transcript 后重投能查重跳过（turn 幂等）。

    世界设定本身（这家是谁、屋里屋外的空间、三姐妹各自客观作息）由 system prompt
    一处承载，USER 层不再拼——避免世界设定两处真相。这里只喂"此刻动态"：上一版
    叙述 / 现在几点 / 唤醒缘由 / 这批动作。

    ``act_batch_text``：act 唤醒时这一批所有人的动作清单（从 PG 读全，对称 life 读
    mailbox）。非空才插入「这一批动作」段——让 world 看到所有人的动作、不只合并闸
    latest-only 透进来的最后一条。heartbeat / self 唤醒没有动作批次，留空、不插这段。
    """
    act_section = (
        f"【这一批要你推演客观结果的动作（所有人）】\n{act_batch_text}\n\n"
        if act_batch_text
        else ""
    )
    user_content = (
        f"{_round_marker(round_id)}\n"
        f"{world_loop_instruction()}\n\n"
        f"【现实此刻】{now_iso}\n"
        f"【你记得的上一版世界叙述】\n{detail}\n\n"
        f"【这次醒来的缘由】{wake_reason}\n\n"
        f"{act_section}"
        "看一眼这个世界，推演此刻它什么样，用 update_world 写下来；该让谁感知到的"
        "客观动静用 notify 投出去；最后用 sleep 定下次多久再看。"
    )
    return [Message(role=Role.USER, content=user_content)]


def _round_already_processed(history: list[Message], round_id: str) -> bool:
    """这轮（round_id）是否已在 session 历史里出现过（turn 幂等查重）。

    同一 durable act 重投得同一 round_id；第一次 run 把带本轮标记的 user 消息
    写进 transcript，重投时这里从已读到的历史里查这行标记，命中即已处理过 → 跳过。
    """
    marker = _round_marker(round_id)
    for m in history:
        if m.role == Role.USER and marker in m.text():
            return True
    return False


def _derive_round_id(tick: WorldTick, now_iso: str) -> str:
    """本轮确定性标识，按**触发源**稳定派生（整轮重放 event_id 幂等命门）。

    round_id 喂进 :func:`app.world.tools.derive_event_id`，整轮重放时同一条动静
    要落同一 id 才能靠 ``deliver_event`` 去重。所以 round_id 不能从 now_iso 派生
    （重投会取新时刻 → 新 event_id → 去重失效）：

      * ``reason == "act"``：动作唤醒经 ``wire(ActPerformed).durable()`` 跨进程，
        world_tick 半途失败会被 durable 重投。用动作的稳定标识 ``act_id`` 派生，
        同一 ActPerformed 重投得同一 round_id → 同 event_id → 去重成功。
      * heartbeat / self：纯 in-process、不会 durable 重投，用现实时刻派生即可
        （不同时刻不同 round，符合"不同唤醒不同轮"的语义）。
    """
    if tick.reason == "act" and tick.act_id:
        seed = f"{tick.lane}\x1fact\x1f{tick.act_id}"
    else:
        seed = f"{tick.lane}\x1f{tick.reason}\x1f{now_iso}"
    return uuid.uuid5(uuid.NAMESPACE_OID, seed).hex


@node
async def world_tick(tick: WorldTick) -> None:
    """world 推演者的唯一入口：被三源唤醒，按 actor 串行化跑一轮（锁覆盖全段）。

    确定性 session_id（make_session_id(lane,"world",今天)）把三源唤醒打到同一个
    transcript key，无锁并发会互相覆盖（读改写竞态）。所以开头按 actor（lane）拿
    一把单飞锁，锁必须覆盖「读历史 → run/工具副作用 → 写回」整段
    （:func:`_run_world_round` 全程在锁内）。

    锁冲突按唤醒源分别处理（兼顾三源、绝不丢动作）：
      * **heartbeat**：吞掉（log + return）。心跳是 10min 保底冗余，正在跑的那轮
        会自己自排 / 下次心跳再补，丢这一次无害。
      * **self**：吞掉（log + return）。自排是 world 自己排的冗余唤醒，正在跑的
        那轮收口时会重排自己的下次醒，丢这一次无害。
      * **act**：抛 ``SingleFlightConflict``，交给上游 act→world 合并闸
        （debounce 的 :func:`world_act_wake`）重排 —— 动作绝不能被丢
        （life 做的事丢了世界就推不动）。
    """
    lane = tick.lane
    lock_key = f"world:{lane}"
    try:
        async with single_flight(lock_key, ttl=WORLD_TICK_LOCK_TTL_SECONDS):
            await _run_world_round(tick, lane=lane)
    except SingleFlightConflict:
        if tick.reason == "act":
            # act 绝不丢：抛回 world_act_wake，它 raise DebounceReschedule 让合并闸
            # 稍后重排这次唤醒。
            logger.info(
                "[world_tick] %s act wake hit lock, reschedule via gate", lane
            )
            raise
        # heartbeat / self：冗余唤醒，吞掉不抛（log 留痕、不静默）。
        logger.info(
            "[world_tick] %s %s wake hit lock, drop (redundant safety/self wake)",
            lane,
            tick.reason,
        )
        return


async def _run_world_round(tick: WorldTick, *, lane: str) -> None:
    """一轮 world 的实际编排（已在 actor 锁内）：对账 → 续接 run → 收口排下次醒。

    一次唤醒：
      1. **先对账补敲遗留信箱**（renotify_unread）—— 纯机械 IO 兜底，先于到点
         gate、先于循环、不依赖循环成功。即使这次 tick 随后被 gate 判废（如长睡
         期间的保底心跳），补敲也已经做过，stranded 信箱不会被 gate 挡住没人补。
      2. 算现实此刻时间（CST），喂给 prompt（world 时间由 update_world 工具落，
         engine 不主动写快照）。
      3. 读自己上一版客观世界叙述；无快照 = 冷启动，缘由告诉 LLM 这是首次醒来、
         由它 update_world 写第一版。
      4. **turn 幂等查重**：load_session 读已有 transcript，若本轮 round_id 标记
         已在历史里（同一 durable act 重投）→ 收口跳过，不再 run、不重复推演。
      5. 把"上一版世界叙述 / 现在几点 / 这批动作"作为 prompt context 喂入（世界设定
         由 system prompt 一处承载，USER 层不拼），用**确定性 session_id 续接**跑
         agent 工具循环：把 session_id 显式传给
         ``Agent.run(session_id=)``，让 world 接着上一轮往下（记得自己这一天 update/
         notify 过啥、几点了）。工具读 ctx 里的 lane + round_id 行动。
      6. 循环自然收口（不再调工具就停）；中途瞬时失败因 max_retries=1 直接抛、
         收口本轮已做的，靠保底心跳 + 下次 renotify 补。
      7. **收口只排下次醒**（fire_self_wake）——世界叙述快照改由 update_world 工具
         在循环里负责写，engine 不再主动落快照。

    session：当天 world 的所有唤醒归到她自己一条按天滚动的 session
    （make_session_id(lane,"world",今天)）；同一个 id 既是 langfuse session 标签
    也是 transcript key，"看到的连续 session"背后真有连续上下文。
    """
    # 现实此刻时间（CST）：gate 到点判定 + 喂 prompt 都用它（gate 比较一律用现实
    # 时间，绝不用会因 gate 停滞的 world_time）。world_time 快照由 update_world 工具
    # 落、engine 不主动写——engine 只把"现在几点"作为推演起点喂给 world。
    now = datetime.now(_CST)
    now_iso = now.isoformat()

    # 信箱对账自愈（先于到点 gate、先于 agent 循环）：补敲该 lane 下所有还有未读
    # event 的 persona。deliver_event 的"落库 + emit 敲门"非原子，敲门撞上瞬时失败时
    # event 会永久躺在信箱里没人读。world 保底心跳纯 in-process、不依赖外部敲门，
    # 所以一定有机会跑——每个 tick 一进来先把遗留的、丢掉的敲门补回来。
    #
    # 关键：补敲放在到点 gate **之前**。renotify_unread 是纯机械 IO 兜底（不经 LLM、
    # 不进世界内容决策、对已读 persona 也无害），不该被到点 gate 挡掉——否则 world
    # 长睡期间每个保底心跳都被 gate 判废、stranded 信箱就永远没人补敲。所以先补敲、
    # 再走 gate；gate 判废仍 early return（但补敲已经做过）。也先于 agent 循环：哪怕
    # 这轮循环抛异常，积压的 stranded 信箱也已先补过敲。不违赤尾宪法。
    await renotify_unread(lane=lane)

    snapshot = await read_world_state(lane=lane)
    cold_start = snapshot is None

    # 到点 gate（阶段 1B Task 1）：self / 心跳唤醒在真正推演前先判此刻作不作数——
    # 未到 next_wake_at 的心跳、未到点或已被覆盖（stale）的 self 一律判废、早返：
    # 不推演、不产新 state，让 world 的长睡意愿真生效（不再被保底心跳拍醒）。补敲
    # 信箱已在 gate 之前做过、不被 gate 挡。外部刺激（act 角色动作）reason 不在 gate
    # 范围、永远放行，立刻打断长睡。
    next_wake_at = snapshot.next_wake_at if snapshot is not None else None
    if not _self_wake_gate_passes(tick, next_wake_at=next_wake_at, now=now):
        logger.info(
            "[world_tick] %s %s wake gated out (now=%s next_wake_at=%s "
            "carried_target=%s): not due / stale, skip deliberation "
            "(mailbox already renotified before gate)",
            lane,
            tick.reason,
            now_iso,
            next_wake_at,
            tick.target_wake_at or "-",
        )
        return

    if cold_start:
        # 冷启动：还没有上一版世界叙述。给 detail 段一个占位文本喂 prompt，由
        # wake_reason 引导 world 推演 + update_world 写第一版（不在 engine 造占位
        # WorldState，世界叙述统一由工具落）。
        detail = "（首次醒来，还没有上一版世界叙述。）"
    else:
        detail = snapshot.detail

    # 本轮确定性标识：派生 event_id 靠它（整轮重放同一条动静同一 id，幂等去重）。
    # round_id 必须从**触发源**稳定派生，不能从 now_iso：world_tick 半途失败被
    # durable 重投时，若 round_id 取新 now_iso → 同 observation 生成新 event_id →
    # deliver_event 去重失效、动静重复投（命门）。
    #   * act 唤醒会 durable 重投 → 用动作稳定标识 act_id 派生，重投得同一 round_id
    #     → 同 event_id → 去重成功。
    #   * heartbeat / self 唤醒纯 in-process、不会 durable 重投 → 用现实时刻派生即可。
    round_id = _derive_round_id(tick, now_iso)

    # session 按角色按天滚动：world 当天所有唤醒归到她自己一条 session。
    session_id = make_session_id(lane, "world", now.strftime("%Y-%m-%d"))

    # turn 幂等：读已有 transcript，若本轮 round_id 已处理过（同一 durable act 重投
    # 得同一 round_id）→ 跳过，不再 run / 不重复推演 / 不重复追加。锁覆盖全段保证
    # load → run → 写回 之间 round 标记不被并发抢写。读不到（过期 / 首次）按空历史
    # 走、正常跑（冷启降级，不报错）。
    history = await load_session(session_id)
    if _round_already_processed(history, round_id):
        logger.info(
            "[world_tick] %s round %s already in transcript, skip (turn idempotent)",
            lane,
            round_id,
        )
        return

    wake_reason = _wake_reason_text(tick, cold_start=cold_start)

    # act 唤醒：从 PG 读这段时间所有 act 全部呈现给 world（对称 life 读 mailbox）。
    # 合并闸是 latest-only、只透进来最后一条 payload，前面几条 act 对 world 等价丢失
    # （命门）。这里读全那一批拼进 prompt，让 world 看到所有人的动作、逐条推演客观
    # 结果。窗口下界 = 触发 act occurred_at - lookback（锚 act、不锚 world_time）。
    # heartbeat / self 唤醒没有动作要呈现，不读。
    act_batch_text = ""
    if tick.reason == "act":
        since_iso = _act_since_cutoff(tick, now)
        recent_acts = await list_recent_acts(lane=lane, since_iso=since_iso)
        act_batch_text = _act_batch_text(recent_acts)

    messages = _world_loop_messages(
        detail=detail,
        now_iso=now_iso,
        wake_reason=wake_reason,
        round_id=round_id,
        act_batch_text=act_batch_text,
    )

    # 工具体读 ctx.features 里的 lane + round_id 行动（lane / round 是机制层的，
    # 不让模型在工具签名里填）。每轮新建 round-scoped 可变 state：FEATURE_SELF_WAKE
    # 让 sleep 把待办 self-wake 写进来（覆盖而非追加），循环收口后读它 emit 至多一条
    # self WorldTick（唤醒风暴命门）。session_id 也塞进 context（langfuse 归类一致）；
    # 续接靠下面显式传给 run。
    context = AgentContext(
        session_id=session_id,
        features={
            "world_lane": lane,
            "world_round_id": round_id,
            FEATURE_SELF_WAKE: {},
        },
    )

    # 跑 agent 工具循环，**显式传 session_id 续接**：run 见到显式 session_id 就从
    # transcript 读历史拼到 messages 前、跑完把本轮（含工具调用与结果）追加写回。
    # 显式 session_id 优先于 context.session_id。max_retries=1 关掉整轮重放：
    # update_world / notify 是 durable 写，一次 model 调用瞬时失败若整轮重放会重放
    # 已执行的 durable 工具（失败语义命门）。
    await Agent(_WORLD_CFG, tools=WORLD_TOOLS).run(
        messages,
        context=context,
        session_id=session_id,
        max_retries=1,
    )

    # 循环收口后 emit 至多一条 self WorldTick（唤醒风暴命门）：sleep 工具把"下次几时
    # 醒"写进 round-scoped FEATURE_SELF_WAKE（覆盖而非追加），这里读最后一次的待办、
    # emit 唯一一条。没调 sleep（无待办）就不 emit，靠 10min 保底心跳兜底。firing
    # 机制收口在工具域（fire_self_wake），engine 只在循环收口处触发。世界叙述快照
    # 已由 update_world 工具在循环里写，engine 收口不再落快照。
    await fire_self_wake(lane=lane, self_wake=context.features.get(FEATURE_SELF_WAKE))


@node
async def heartbeat_to_world_tick(_tick: WorldHeartbeatTick) -> None:
    """把保底心跳的 ``WorldHeartbeatTick`` 翻成 ``WorldTick(reason="heartbeat")``。

    这是时间源 → world 的"变速箱"：时间源喂的单字段 ``WorldHeartbeatTick``（满足
    框架的单字段 ts 约定）经这个机械翻译节点补上 lane + reason，emit 一条
    ``WorldTick`` 经 in-process 边接回 :func:`world_tick`。

    lane 显式从**进程级部署泳道**取——interval 源循环的 context lane 是 ``None``
    （时间源不携带 request lane），所以这里不能靠 context 注入，必须自己读
    ``current_deployment_lane()``。prod（``LANE`` 未设 → None）归一到 ``"prod"``，
    与 infra 各处 ``lane or "prod"`` 口径一致；``WorldTick.lane`` 是必填非空 Key，
    且它就是 world 快照 / 信箱的分区键，整条 world/life 回环的 lane 都由这一处心跳
    种下（自排、动作回灌的 lane 都从 ``WorldTick.lane`` 一路传下去）。

    心跳只决定"何时醒"、绝不进世界内容决策——这个翻译节点只是机械接线（赤尾
    设计宪法）。手动 ``emit`` 而非 @node 自动 emit：让翻译目标显式可读，且测试
    能 monkeypatch 模块级 ``emit``。
    """
    await emit(
        WorldTick(
            lane=current_deployment_lane() or "prod",
            reason="heartbeat",
        )
    )


@node
async def act_to_world_tick(act: ActPerformed) -> None:
    """把 life 回灌的 ``ActPerformed`` 翻成 ``ActWorldTick``（进 60s 合并闸）。

    act 唤醒 world 不再直接打 ``WorldTick``，而是先翻成 transient ``ActWorldTick``
    走 60s debounce 合并闸（world 被唤醒最小间隔 1min，短于 1min 的连续 act 合并成
    一次唤醒）。闸后的 :func:`world_act_wake` 再翻成 ``WorldTick(reason="act")``
    打到 :func:`world_tick`。

    life emit 的动作字段（act_id / persona_id / description / occurred_at）透传：
    ``act_id`` 是动作的稳定标识，闸后 world_tick 用它派生本轮 round_id —— 同一条
    ActPerformed 被 durable 重投时得同一 round_id → 同 event_id / 同 transcript round
    标记 → 去重 + turn 幂等（命门）。

    这条边的 durable 不变：上游 ``wire(ActPerformed).to(act_to_world_tick).durable()``
    承载 durable 跨进程（life 进程 → world 进程），ActPerformed 的 ``(lane, act_id)``
    自然键 durable 幂等不被破坏（合并闸放在闸后的 transient 信号上，不动这条 durable
    边）。

    手动 ``emit`` 而非 @node 自动 emit：让翻译目标显式可读，且测试能 monkeypatch
    模块级 ``emit``。
    """
    await emit(
        ActWorldTick(
            lane=act.lane,
            act_id=act.act_id,
            act_persona_id=act.persona_id,
            act_description=act.description,
            act_occurred_at=act.occurred_at,
        )
    )


@node
async def world_act_wake(wake: ActWorldTick) -> None:
    """合并闸（debounce）后的 act 唤醒：翻成 ``WorldTick(reason="act")`` 喂 world。

    ``wire(ActWorldTick).debounce(60s, per-lane).to(world_act_wake)`` 把 1min 窗口内
    的连续 act 合并成一次：闸到点只 fire 这一个 ``world_act_wake``，它把最后那条 act
    的内容翻成 ``WorldTick(reason="act")`` **直接调** world_tick。

    直接 ``await world_tick(...)``（不经 emit）的关键：world_tick 撞锁时对 act 抛
    ``SingleFlightConflict``，这里捕获后 ``raise DebounceReschedule(wake)`` 交给
    debounce handler 重排 —— 动作绝不丢（命门）。若经 in-process emit，异常虽也能
    冒泡上来，但直接调让"撞锁 → 重排"这条动作不丢的路径显式可读。
    """
    try:
        await world_tick(
            WorldTick(
                lane=wake.lane,
                reason="act",
                act_id=wake.act_id,
                act_persona_id=wake.act_persona_id,
                act_description=wake.act_description,
                act_occurred_at=wake.act_occurred_at,
            )
        )
    except SingleFlightConflict:
        # world 正忙（另一轮在跑）：交回合并闸稍后重排这次 act 唤醒，绝不丢。
        from app.runtime.debounce import DebounceReschedule

        raise DebounceReschedule(wake) from None
