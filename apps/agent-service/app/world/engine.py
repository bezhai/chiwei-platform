"""world engine 节点 — pull 范式（world 按自排节奏醒来、批量 pull act）.

world 是这个世界的推演层，不是导演。它被**两源唤醒**（都走到点 gate），每次唤醒
走同一条回路：先对账信箱自愈 → 算现实此刻时间（CST）→ 读自己上一版客观世界叙述
（无快照=冷启动）→ **从上次消费游标之后批量 pull 这段时间攒下的 act** → 把"上一版
世界叙述 / 现在几点 / 这批角色动作"作为 prompt context 一次喂全（世界设定本身——
这家是谁、各自客观作息——由 system prompt 一处承载，USER 层不再重复拼），**跑一个
agent 工具循环**：world 在循环里推演世界此刻什么样、用 update_world 写下世界叙述、
对收到的角色动作推演客观结果、用 notify 把够得着的角色推演出来投客观动静、用
sleep 定下次再看 → 推演成功收口后把消费游标推进到本批末尾。

它不再"填一张表"返回结构化大对象。"世界此刻什么样""谁够得着一条动静""产什么
客观动静""睡多久"全在循环里由 LLM 用工具表达——把一个被训练成"连续调工具行动"
的模型用它擅长的方式驱动，世界由它推演、不再凝固。

新范式与旧设计的根本差别：旧设计 world 是导演 / 裁决者（move_persona 替角色挪
位置、emit_event 按"谁在某 room"广播、被 intent 唤醒后裁准 / 拒绝角色意图）。
现在 world 退成推演者——它绝不替角色决定她想做什么 / 怎么想 / 什么情绪（那是角色
自己的事），它对角色动作（act）只推演"客观上发生了什么"、绝不批准 / 拒绝她想不想
做（她几乎总能做到，除非客观世界里有硬冲突）。谁够得着一条动静由 world 推演（在
厨房的人闻得到厨房的香味、在学校的够不着），不是查表。

pull 范式（act 不再唤醒 world）：life 做完一件事直接 ``insert_idempotent`` 落
``ActPerformed`` 到 PG，**不唤醒 world**。world 不被 act 拽起来，频率主权完全交回
它自己的 sleep；它每次推演（不分唤醒源）都从游标 pull "上次消费以来攒的所有 act"
一并推演，推完把游标推进到本批末尾。act 仍一条不丢（持久化在 PG 等 world 来读），
只是不再每条都把 world 拽起来——这把"三姐妹一刻不停做事 → world 被每分钟拽一次
全量推演"的高频烧钱压回到 world 自排的节奏。

两个唤醒源（都打到 ``world_tick`` 节点，靠 :class:`WorldTick` 的 ``reason``
区分，都走到点 gate）：

  1. **保底心跳**：``Source.interval(WORLD_HEARTBEAT_SECONDS)`` 每 10 分钟喂一条
     单字段 :class:`WorldHeartbeatTick`（满足框架时间源的单字段 ts 约定），由
     :func:`heartbeat_to_world_tick` 翻成 ``WorldTick(reason="heartbeat")`` 踹
     world 一下（世界时钟的滴答，只叫它看一眼）。这钉死最长停摆——所有 life
     都靠 world 启动，world 睡死世界就死。时间源不直接喂 ``WorldTick``：那会在
     源循环 ``_build_payload(WorldTick(ts=...))`` 处 ValidationError 杀 Pod
     （``WorldTick`` 无 ts、缺必填 lane），world 在生产里永远起不来。
  2. **自排下次醒**（主节奏）：world 在循环里调 ``sleep(seconds)`` 工具
     （:func:`app.world.tools.sleep`）决定下次几时醒，收口经
     :func:`app.world.tools.fire_self_wake` ``emit_delayed`` 一个
     ``WorldTick(reason="self")``，到期经 in-process 边接回本节点。sleep 上下限
     60s~1h，超限工具报错喂回模型重调，不静默夹。

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
快照（经 update_world 工具 / ``app.world.state``）、``insert_idempotent`` act 落库
（经 life 侧 ``perform_act``）、``Agent.run`` agent 循环。本节点只用现成原语，不改
runtime / core。

wiring（interval 心跳源 → WorldHeartbeatTick → heartbeat_to_world_tick；WorldTick
纯 in-process 接回 world_tick；act **没有 wire**——pull 范式下 act 不唤醒 world）
在 ``app/wiring/life_dataflow.py`` 收口。本模块提供 world 节点 + 唤醒信号 domain
+ agent 循环组装 + 心跳翻译节点；world 的工具集在 ``app/world/tools.py``。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.session import load_session
from app.agent.trace import collect_usage, make_session_id
from app.data.queries.acts import list_recent_acts
from app.data.queries.mailbox import renotify_unread
from app.domain.thinking_cost import record_round_cost
from app.domain.world_events import ActPerformed
from app.infra import cst_time
from app.runtime.data import Data, Key
from app.runtime.emit import emit  # module-level so tests can monkeypatch
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node
from app.runtime.single_flight import SingleFlightConflict, single_flight
from app.world.state import advance_act_cursor, read_world_state
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
# 循环多轮）。确定性 session_id 把两源唤醒打到同一个 transcript key，无锁并发会
# 互相覆盖；所以 world 像 life 一样按 actor（lane）串行化，锁覆盖「读历史 →
# run/工具副作用 → 写回」整段。锁只是基建并发控制（不替 agent 决策、不违赤尾
# 宪法），TTL 到期后哪怕原 holder 还在跑、新 holder 也能进，token-CAS 释放保证
# 不误删别人的锁。
WORLD_TICK_LOCK_TTL_SECONDS = 600

# 一轮 pull act 的读取上限（防单轮 context 爆炸的护栏）。world 最长可睡 1h，期间
# life 可能攒几十上百条 act，一次全塞 prompt 会把"高频烧钱"变成"单轮 context 爆炸
# 甚至每轮失败重试"。所以一轮最多读 N 条（按发生顺序取最早的、游标只推进到已读末尾，
# 剩下的下轮接着读、不截断丢弃）。读满 N 条时缘由文本告诉 world"还有积压"，由 world
# **自己排短 sleep** 来尽快消化——决策仍在 world 手里，这不是"攒够 N 条提前唤醒
# world"的机制提前拉起（频率主权交给 world 自己的 sleep）。
WORLD_ACT_PULL_LIMIT = 10

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
    """world 的唤醒信号（两源都打这个 Data 到 ``world_tick`` 节点）。

    transient——只当唤醒信号，世界内容在 durable 快照 / 信箱 / act 表里。``reason``
    区分唤醒源（heartbeat / self）。pull 范式下 act 不再是唤醒源，所以没有 act_*
    字段——act 内容由 world 醒来按游标从 PG pull。

    纯 in-process：``WorldTick`` 不直接挂时间源（时间源的形态约束由
    :class:`WorldHeartbeatTick` 承载），它只承载两种 in-process 来源——心跳翻译
    （:func:`heartbeat_to_world_tick`）、``world_tick`` 自排（``emit_delayed``）。
    """

    lane: Annotated[str, Key]
    reason: str = "heartbeat"          # heartbeat | self
    # reason==self 时：这条 self 唤醒被排时的目标唤醒时刻（现实 CST aware ISO）。
    # 到期时与 WorldState 当前 next_wake_at 比对判 stale —— 不一致说明这条 self 已被
    # 新自排覆盖、作废（阶段 1B 到点 gate）。life 自排照搬此字段。
    target_wake_at: str = ""

    class Meta:
        transient = True


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


# 走到点 gate 的唤醒源：self（world 自排）与 heartbeat（保底心跳）。pull 范式下
# 这就是全部唤醒源——act 已退出唤醒语义（act 只落库、world 醒来按游标 pull），所以
# 不再有"外部刺激永远放行"那一支；任何唤醒都走 gate，频率主权交给 world 自己的 sleep。
_GATED_REASONS = frozenset({"self", "heartbeat"})


def _self_wake_gate_passes(
    tick: WorldTick,
    *,
    next_wake_at: str | None,
    now: datetime,
) -> bool:
    """到点 gate（阶段 1B Task 1）：判这次唤醒此刻作不作数。

    pull 范式下两个唤醒源（self / heartbeat）都走 gate，判两件事——

      1. **到点没到**：现实 ``now`` ≥ ``next_wake_at`` 才作数（比较一律用现实
         aware 时间，**不用 world_time**：world_time 会因 gate 停滞、拿它判到点
         会永远不醒）。
      2. **这条唤醒还作不作数（仅 self）**：self 携带它被排时的目标时刻
         ``tick.target_wake_at``，到期时与 state 当前 ``next_wake_at`` 比对，不一致
         说明已被新自排覆盖、判废（旧 self 到期不能误触发推演）。

    ``next_wake_at`` 为 None（从没排过：首轮 / 冷启 / 只 update_world 没 sleep）时
    心跳放行（别卡死首轮）；self 不该在没排过时来，None 时 self 也判废（没有合法
    目标可比对）。``_GATED_REASONS` 之外的 reason（不该出现）保守放行兜底。

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


def _wake_reason_text(tick: WorldTick, *, cold_start: bool, has_backlog: bool) -> str:
    """把唤醒信号翻成给 world 循环的缘由文本。

    pull 范式下缘由不分 act/非 act —— 任何唤醒都从游标 pull 这段时间攒下的 act
    （内容在 :func:`_act_batch_text` 拼的批次清单里）。``has_backlog`` 为真（这一批
    读满了 N 条上限、还有动作没读完）时在缘由里告诉 world，**由她自己排短 sleep**
    来尽快回来消化剩下的（决策在 world 手里，不是机制提前唤醒她）。
    """
    if cold_start:
        return (
            "世界冷启动：这是 world 首次醒来，还没有上一版世界叙述。请按现实当前"
            "时间 + 你已知的这个世界（谁住在这、各自客观上大致的一天），推演此刻"
            "世界大致什么样（谁大概在哪、在干嘛、什么氛围），用 update_world 写下"
            "第一版世界叙述。"
        )
    base = (
        "上一轮你自排的提前卡点到了，再看一眼世界，推演这段时间攒下来的动作。"
        if tick.reason == "self"
        else "例行看一眼世界，推演这段时间攒下来的动作。"
    )
    if has_backlog:
        base += (
            "（这段时间积压的动作太多、这一批没读完，剩下的还在排队——如果你想尽快"
            "把它们消化掉，可以把这次 sleep 排短一点、早点回来接着推演。）"
        )
    return base


def _act_batch_text(acts: list[ActPerformed]) -> str:
    """把这一批 pull 到的 act 拼成一段清单文本喂给 world（对称 life 读 mailbox）。

    呈现每个人这段时间做了什么，让 world 看到这一批所有人的动作、逐条推演客观结果。
    空批次给一句兜底（醒来时这段没有新动作、纯 self / 心跳推进世界）。
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
#
# marker 编码 round_id **+ 本批终点游标 ``(created_at, act_id)``**（必改 2 命门）：
# 幂等绑"游标起点"派生的 round_id，但命中后要把游标推进到这一批**真正推完到的终点**
# ——这个终点记在 marker 里。崩溃+扩批时（advance 没落、期间新增 act 让批变大）重读
# 命中同一 round_id（起点派生），从 marker 取出当时记的终点（旧批末尾、不是扩出的新
# 批末尾）推进游标 → 跳过、不重推、不把扩出的新 act 误并进上一轮。
# 格式（非空批）：``[world-round:<round_id>|end:<created_at>|<act_id>]``
# 格式（空批次）：``[world-round:<round_id>|end:-]``（无终点游标可推进）。
# created_at 是 ISO（含 ``:`` ``+`` ``-`` ``T``，不含 ``|`` ``]``），act_id 是 UUID
# 串（只有 hex + ``-``，不含 ``|`` ``]``），所以 ``|`` 作分隔安全、能稳定解析回。
_ROUND_MARKER_PREFIX = "[world-round:"
_MARKER_RE = re.compile(r"\[world-round:(?P<rid>[^|\]]+)\|end:(?P<end>[^\]]*)\]")


def _round_marker(
    round_id: str,
    *,
    end_created_at: str | None,
    end_act_id: str | None,
) -> str:
    """编码本轮标记：round_id + 本批终点游标 ``(created_at, act_id)``。

    非空批次传 ``(end_created_at, end_act_id)``（本批末尾游标，命中时推进到它）；
    空批次传 ``(None, None)``（无终点可推进，编码成 ``end:-``）。
    """
    if end_created_at is None or end_act_id is None:
        return f"{_ROUND_MARKER_PREFIX}{round_id}|end:-]"
    return f"{_ROUND_MARKER_PREFIX}{round_id}|end:{end_created_at}|{end_act_id}]"


def _world_loop_messages(
    *,
    detail: str,
    now_iso: str,
    wake_reason: str,
    round_id: str,
    act_batch_text: str = "",
    end_created_at: str | None = None,
    end_act_id: str | None = None,
) -> list[Message]:
    """把"上一版世界叙述 / 现在几点 / 这批动作"拼成喂给循环的 user 消息。

    ``detail`` 是上一版世界叙述（冷启动时是一句"首次醒来、还没有上一版世界叙述"的
    占位文本）。``now_iso`` 是 engine 算的现实此刻时间（CST）。这里把当前客观 context
    一次喂全，让 world 在循环里推演 + 用 update_world 写新的世界叙述。开头印一行本轮
    标记（``round_id`` + 本批终点游标 ``(end_created_at, end_act_id)``），写回 transcript
    后重投能查重跳过（turn 幂等）、并据终点游标把游标补推到上一轮真正推完的位置。

    世界设定本身（这家是谁、屋里屋外的空间、三姐妹各自客观作息）由 system prompt
    一处承载，USER 层不再拼——避免世界设定两处真相。这里只喂"此刻动态"：上一版
    叙述 / 现在几点 / 唤醒缘由 / 这批动作。

    ``act_batch_text``：这一批从游标 pull 到的所有人的动作清单（对称 life 读
    mailbox）。非空才插入「这一批动作」段——让 world 看到这段时间攒下的所有动作。
    这段时间没有新 act（纯 self / 心跳推进世界）时留空、不插这段。

    ``end_created_at`` / ``end_act_id``：本批终点游标（落库时刻 + act_id），编进 marker
    供重读命中时推进游标。空批次传 None（marker 编码成 ``end:-``，无终点可推进）。
    """
    act_section = (
        f"【这一批要你推演客观结果的动作（所有人）】\n{act_batch_text}\n\n"
        if act_batch_text
        else ""
    )
    user_content = (
        f"{_round_marker(round_id, end_created_at=end_created_at, end_act_id=end_act_id)}\n"
        f"{world_loop_instruction()}\n\n"
        f"【现实此刻】{now_iso}\n"
        f"【你记得的上一版世界叙述】\n{detail}\n\n"
        f"【这次醒来的缘由】{wake_reason}\n\n"
        f"{act_section}"
        "看一眼这个世界，推演此刻它什么样，用 update_world 写下来；该让谁感知到的"
        "客观动静用 notify 投出去；最后用 sleep 定下次多久再看。"
    )
    return [Message(role=Role.USER, content=user_content)]


def _round_already_processed(
    history: list[Message], round_id: str
) -> tuple[str, str] | None:
    """这轮（round_id）是否已在 session 历史里处理过 → 返回它记的终点游标 or None。

    第一次 run 把带本轮标记的 user 消息写进 transcript，marker 里编码了 round_id +
    本批真正推完到的**终点游标 ``(created_at, act_id)``**。重读时这里从已读到的历史
    里找这个 round_id 的 marker：

      * 命中且 marker 记了终点游标（非空批）→ 返回 ``(created_at, act_id)``。调用方
        据此把游标推进到 marker 记的终点（不是当前批末尾——崩溃+扩批时当前批可能更
        大，但只能推进到上一轮真正推完的终点），然后跳过 run（不重推）。
      * 命中但 marker 是空批次（``end:-``，无终点游标）→ 返回 None（空批次本就不推进）。
      * 未命中（首次 / 过期）→ 返回 None（正常 run）。

    return None 既表示"没处理过"也表示"处理过但无终点可推进"——两种都不该推进游标、
    其中前者还要 run、后者跳过。调用方靠"先查 round_id 在不在历史"区分，见
    :func:`_run_world_round`。
    """
    for m in history:
        if m.role != Role.USER:
            continue
        for match in _MARKER_RE.finditer(m.text()):
            if match.group("rid") != round_id:
                continue
            end = match.group("end")
            if end == "-":
                return None
            created_at, _, act_id = end.rpartition("|")
            if not created_at or not act_id:
                return None
            return (created_at, act_id)
    return None


def _round_in_history(history: list[Message], round_id: str) -> bool:
    """本轮 round_id 的 marker 是否已在历史里（无论终点游标是否为空批次）。

    ``_round_already_processed`` 对"命中但空批次"返回 None（与未命中同值），所以推进
    游标的判定不能只看它的返回值——还要这个"在不在历史"的布尔判 turn 幂等是否命中。
    """
    for m in history:
        if m.role != Role.USER:
            continue
        for match in _MARKER_RE.finditer(m.text()):
            if match.group("rid") == round_id:
                return True
    return False


def _derive_round_id(
    lane: str,
    *,
    cursor_created_at: str | None,
    cursor_act_id: str | None,
    has_acts: bool,
    now_iso: str,
) -> str:
    """本轮确定性标识，按**游标起点**稳定派生（崩溃扩批仍同 round_id 的命门）。

    round_id 喂进 :func:`app.world.tools.derive_event_id`：整轮里同一条动静要落同一
    id 才能靠 ``deliver_event`` 去重；失败 / 崩溃重读要命中同一 round_id 才能靠
    transcript turn 幂等（``_round_already_processed``）跳过重复推演。必改 2 把派生
    源从"本批 act 集合"改成"游标起点"——

      * **批次非空**：从**游标起点 ``(cursor_created_at, cursor_act_id)``** 稳定派生
        （**不从批集合、不用 now**）。绑批集合的旧实现有命门：advance_act_cursor 崩溃
        后只要新增 act、批集合变大、round_id 就变 → turn 幂等失效 → 旧 act 被重复
        推演、可能重复 notify。绑游标起点后，起点不变 round_id 就不变（哪怕崩溃期间
        批集合扩大）→ marker 仍命中、推进到 marker 记的旧终点、跳过、不重推。
      * **冷启动（游标为 None）非空批**：用固定 cold seed（``lane + ":cold"``），**不用
        now**——冷启动崩溃重读时游标仍是 None，用 now 派生会变 round_id、turn 幂等失效；
        固定 cold seed 让冷启动崩溃重读得同 round_id。
      * **批次空**（醒来没新 act、纯 self / 心跳推进世界）：从 now 派生（不同时刻不同
        轮），允许 world 纯推进世界叙述、每次都是新 round 不被误幂等掉。
    """
    if not has_acts:
        seed = f"{lane}\x1fempty\x1f{now_iso}"
    elif cursor_created_at is None or cursor_act_id is None:
        # 冷启动游标：固定 cold seed（不用 now），冷启动崩溃重读同 round_id。
        seed = f"{lane}\x1fcold"
    else:
        # 从游标起点派生：起点不变 → round_id 不变（崩溃扩批仍命中）。
        seed = f"{lane}\x1fcursor\x1f{cursor_created_at}\x1f{cursor_act_id}"
    return uuid.uuid5(uuid.NAMESPACE_OID, seed).hex


@node
async def world_tick(tick: WorldTick) -> None:
    """world 推演者的唯一入口：被两源唤醒，按 actor 串行化跑一轮（锁覆盖全段）。

    确定性 session_id（make_session_id(lane,"world",今天)）把两源唤醒打到同一个
    transcript key，无锁并发会互相覆盖（读改写竞态）。所以开头按 actor（lane）拿
    一把单飞锁，锁必须覆盖「读历史 → run/工具副作用 → 写回」整段
    （:func:`_run_world_round` 全程在锁内）。

    锁冲突（heartbeat / self 都是冗余唤醒）一律吞掉（log + return）：心跳是 10min
    保底冗余，正在跑的那轮会自己自排 / 下次心跳再补；self 是 world 自己排的冗余
    唤醒，正在跑的那轮收口时会重排自己的下次醒。丢这一次无害——act 不再是唤醒源
    （act 落 PG 等 world 来 pull），所以没有"动作绝不丢需 reschedule"那条路径了。
    """
    lane = tick.lane
    lock_key = f"world:{lane}"
    try:
        async with single_flight(lock_key, ttl=WORLD_TICK_LOCK_TTL_SECONDS):
            await _run_world_round(tick, lane=lane)
    except SingleFlightConflict:
        # heartbeat / self：冗余唤醒，吞掉不抛（log 留痕、不静默）。
        logger.info(
            "[world_tick] %s %s wake hit lock, drop (redundant safety/self wake)",
            lane,
            tick.reason,
        )
        return


async def _run_world_round(tick: WorldTick, *, lane: str) -> None:
    """一轮 world 的实际编排（已在 actor 锁内）：对账 → gate → pull act → run → 收口推进游标 + 排下次醒。

    一次唤醒：
      1. **先对账补敲遗留信箱**（renotify_unread）—— 纯机械 IO 兜底，先于到点
         gate、先于循环、不依赖循环成功。即使这次 tick 随后被 gate 判废（如长睡
         期间的保底心跳），补敲也已经做过，stranded 信箱不会被 gate 挡住没人补。
      2. 算现实此刻时间（CST），喂给 prompt（world 时间由 update_world 工具落，
         engine 不主动写快照）。
      3. 读自己上一版客观世界叙述 + act 消费游标；无快照 = 冷启动，缘由告诉 LLM
         这是首次醒来、由它 update_world 写第一版。
      4. **从游标 pull act**（pull 范式）：任何唤醒源都从 ``(占游标 created_at,
         act_id)`` 之后批量读这段时间攒下的 act（最多 N=WORLD_ACT_PULL_LIMIT 条、
         按落库顺序取最早的、读满则在缘由文本告知 world 有积压）。游标用 created_at
         （单调落库序）不漏 out-of-order act。round_id 批次非空时从**游标起点**稳定
         派生（崩溃扩批仍同 round_id）、空批次从 now 派生。
      5. **turn 幂等查重**：load_session 读已有 transcript，若本轮 round_id 标记
         已在历史里（失败 / 崩溃重读）→ 推进游标到 **marker 记的终点** 后跳过，不再
         run、不重复推演（不是推进到当前批末尾——崩溃+扩批时当前批可能更大）。
      6. 把"上一版世界叙述 / 现在几点 / 这批动作"作为 prompt context 喂入（世界设定
         由 system prompt 一处承载，USER 层不拼），marker 编 round_id + 本批终点游标，
         用**确定性 session_id 续接**跑 agent 工具循环：把 session_id 显式传给
         ``Agent.run(session_id=)``。工具读 ctx 里的 lane + round_id 行动。
      7. 循环自然收口（不再调工具就停）；中途瞬时失败因 max_retries=1 直接抛、
         **游标不推进**（失败不推进、下轮重读这批，act 不丢）。
      8. **收口推演成功才推进游标到本批末尾 ``(created_at, act_id)`` + 排下次醒**
         （advance_act_cursor + fire_self_wake）——世界叙述快照改由 update_world
         工具在循环里负责写。

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

    # 到点 gate（阶段 1B Task 1）：pull 范式下两源（self / 心跳）都走 gate，在真正
    # 推演前先判此刻作不作数——未到 next_wake_at 的心跳、未到点或已被覆盖（stale）的
    # self 一律判废、早返：不推演、不产新 state、不 pull act，让 world 的长睡意愿真
    # 生效（不再被保底心跳拍醒）。补敲信箱已在 gate 之前做过、不被 gate 挡。
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

    # 从游标 pull act（pull 范式核心）：任何唤醒源都从 WorldState 当前 act 游标之后
    # 批量读这段时间攒下的 act，一轮最多读 N 条（按落库顺序取最早的、剩下下轮接着读、
    # 不截断丢弃）。游标为 None（冷启动 / 从没消费过）时读全既有。游标用 created_at
    # （单调落库序）而非 occurred_at（做事时刻、与落库顺序可乱序）—— out-of-order
    # 漏读命门见 acts.py。list_recent_acts 返回 list[tuple[ActPerformed, created_at]]。
    cursor_created_at = snapshot.act_cursor_created_at if snapshot is not None else None
    cursor_act_id = snapshot.act_cursor_act_id if snapshot is not None else None
    recent = await list_recent_acts(
        lane=lane,
        cursor_created_at=cursor_created_at,
        cursor_act_id=cursor_act_id,
        limit=WORLD_ACT_PULL_LIMIT,
    )
    acts = [a for a, _created_at in recent]
    has_backlog = len(recent) >= WORLD_ACT_PULL_LIMIT
    act_batch_text = _act_batch_text(acts) if acts else ""

    # 本批终点游标 ``(created_at, act_id)``：成功收口推进游标 / marker 记终点都用它。
    # 非空批 = 最后一条（list_recent_acts 按 (created_at, act_id) 升序）；空批 = None。
    if recent:
        last_act, last_created_at = recent[-1]
        batch_end_created_at: str | None = last_created_at
        batch_end_act_id: str | None = last_act.act_id
    else:
        batch_end_created_at = None
        batch_end_act_id = None

    # 本轮确定性标识：派生 event_id / turn 幂等查重靠它（整轮里同一条动静同一 id、
    # 失败 / 崩溃重读同一 round → 幂等去重）。必改 2：批次非空从**游标起点**稳定派生
    # （冷启动游标 None 用固定 cold seed、不用 now）、空批次从 now 派生（命门：见
    # _derive_round_id）。绑游标起点而非批集合，崩溃后批扩大 round_id 仍不变。
    round_id = _derive_round_id(
        lane,
        cursor_created_at=cursor_created_at,
        cursor_act_id=cursor_act_id,
        has_acts=bool(acts),
        now_iso=now_iso,
    )

    # session 按角色按天滚动：world 当天所有唤醒归到她自己一条 session。
    session_id = make_session_id(lane, "world", now.strftime("%Y-%m-%d"))

    # turn 幂等：读已有 transcript，若本轮 round_id 已处理过（失败 / 崩溃重读得同一
    # round_id）→ 跳过，不再 run / 不重复推演 / 不重复追加。锁覆盖全段保证 load →
    # run → 写回 之间 round 标记不被并发抢写。读不到（过期 / 首次）按空历史走、正常
    # 跑（冷启降级，不报错）。**跳过前推进游标到 marker 记的终点**：transcript 里有
    # 标记说明这轮上一次已成功推演过、只是游标推进没落（进程在两步之间挂了）；现在
    # 把游标补推到 **marker 记的终点**（不是当前批末尾——崩溃+扩批时当前批可能更大，
    # 但只能推进到上一轮真正推完的终点），否则会永远重读同一起点、再也读不到新 act
    # （liveness 命门）。
    history = await load_session(session_id)
    if _round_in_history(history, round_id):
        marker_end = _round_already_processed(history, round_id)
        logger.info(
            "[world_tick] %s round %s already in transcript, "
            "advance cursor to marker end %s, skip (turn idempotent)",
            lane,
            round_id,
            marker_end,
        )
        if marker_end is not None:
            await advance_act_cursor(
                lane=lane, created_at=marker_end[0], act_id=marker_end[1]
            )
        return

    wake_reason = _wake_reason_text(
        tick, cold_start=cold_start, has_backlog=has_backlog
    )

    messages = _world_loop_messages(
        detail=detail,
        now_iso=now_iso,
        wake_reason=wake_reason,
        round_id=round_id,
        act_batch_text=act_batch_text,
        end_created_at=batch_end_created_at,
        end_act_id=batch_end_act_id,
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
    # 已执行的 durable 工具（失败语义命门）。中途失败直接抛 —— 下面的收口（推进游标
    # + 排下次醒）都不会执行，游标不推进、下轮重读这批（失败不推进、act 不丢）。
    #
    # **观测刀**：用 collect_usage() 把 run 包住，截下本轮所有 LLM 调用的 token 用量
    # 落 durable PG（不依赖会系统性丢 trace 的 langfuse）。usage 来自 LLM response，
    # 经 adapter 的 span.update → trace 累加器汇进 collector，run 完读 usage 落库。
    with collect_usage() as usage:
        await Agent(_WORLD_CFG, tools=WORLD_TOOLS).run(
            messages,
            context=context,
            session_id=session_id,
            max_retries=1,
        )

    # 本轮 token 落 durable PG（actor = "world"），best-effort 吞掉失败：成本观测是
    # 旁路，绝不能因为记成本失败把一轮真实推演搞成失败重投。落库失败只 log，下面的收口
    # （推进游标 + 排下次醒）照常进行（swallow 语义在 record_round_cost 里）。
    await record_round_cost(
        lane=lane,
        actor="world",
        round_id=round_id,
        usage=usage,
        observed_at=now_iso,
    )

    # 推演成功收口才把游标推进到本批末尾的 ``(created_at, act_id)``（pull 范式命门）：
    # 失败时上面的 run 已抛、不会走到这里，游标保持不动、下轮重读这批。空批次不推进
    # （batch_end_* 为 None、没读到东西可推进）。游标用 created_at（落库序）不漏。
    if batch_end_created_at is not None and batch_end_act_id is not None:
        await advance_act_cursor(
            lane=lane, created_at=batch_end_created_at, act_id=batch_end_act_id
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
    种下（自排、act 落库的 lane 都从 ``WorldTick.lane`` 一路传下去）。

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
