"""world engine 节点 — Task 2（agent 工具循环）.

world 是这个世界的发动机。它被**三源唤醒**，每次唤醒走同一条回路：先对账信箱
自愈 → world_time 跟现实走 → 读自己的客观快照（无快照=冷启动）→ 把"谁在哪 /
现在几点 / 这家作息节律 / 刚才大致光景"作为 prompt context 一次喂全，**跑一个
agent 工具循环**：world 在循环里连续调 move_persona / emit_event / sleep 推进
世界，平淡时段也主动产 event → 循环收口后落一版只含 world_time 的快照。

它不再"填一张表"返回结构化大对象。"够不够格成 event""谁该感知""挪谁""睡多久"
全在循环里由 LLM 用工具表达——把一个被训练成"连续调工具行动"的模型用它擅长的
方式驱动，平淡时段也持续产生活的质感，世界不再凝固。

世界"动起来"靠两条驱动：① 节律驱动（到点该上学/放学/吃饭，world 按客观边界
move_persona 并 emit_event）；② 意图裁决驱动（reason="intent" 时 life 说"我想去
厨房"，world 判断合理就 move_persona 并 emit_event）。谁移动、产不产 event、裁
不裁准意图——全由 LLM 在循环里判断，代码只提供工具、忠实落它调工具的副作用。

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
     （:func:`app.world.tools.sleep`）决定下次几时醒，工具 ``emit_delayed`` 一个
     ``WorldTick(reason="self")``，到期经 in-process 边接回本节点。sleep 上限
     1h（``WORLD_SLEEP_MAX_SECONDS``），超限工具报错喂回模型重调，不静默夹。
  3. **life 回灌的意图**：life emit ``IntentRaised`` → :func:`intent_to_world_tick`
     翻成 ``WorldTick(reason="intent")`` 打到这个节点，world 被唤醒去裁决。

赤尾设计宪法（硬约束）：
  * "够不够格成 event""谁该感知""客观世界变没变""睡多久"全由 LLM 在循环里用
    工具判断——代码里没有任何阈值 / 计数器 / 随机池 / if 分支替它决策。10 分钟
    心跳 / sleep 上限只决定"何时醒 / 别睡死"，绝不进入世界内容的决策。
  * world 只做"客观事实 → 各位置客观可感形态"的感官投影，**绝不碰情绪 / 主观
    解读**（那是 life 的事）。这条由喂 LLM 的 :func:`world_loop_instruction` 在
    prompt 层钉死。
  * 信息差产生侧过滤：event 锚定房间，只投给该房间当前在场的 persona（不为不
    在场的人产 event）。这条由 :func:`app.world.tools.emit_event` 工具落实。

失败语义命门：循环调 ``Agent.run`` 必须传 ``max_retries=1``——core 的 ``run``
把整轮 ReAct 包在 ``@retry`` 里，一次 model 调用瞬时失败会整轮重放、重放已执行
的 durable 工具（move/emit 是 durable 写）。关掉整轮重放后中途失败就抛、收口本
轮已做的，靠 10min 保底心跳 + 开头的 ``renotify_unread`` 下次补（决策 3）。

框架原语：``Source.interval`` 心跳、``emit_delayed`` 自排（经 sleep 工具）、
``deliver_event`` 投递（经 emit_event 工具）、``insert_append`` /
``select_latest`` 快照（经 ``app.world.state``）、``Agent.run`` agent 循环。本
节点只用现成原语，不改 runtime / core。

wiring（interval 心跳源 → WorldHeartbeatTick → heartbeat_to_world_tick；
IntentRaised → intent_to_world_tick；WorldTick 纯 in-process 接回 world_tick）
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
from app.data.queries.intents import list_recent_intents
from app.data.queries.mailbox import renotify_unread
from app.domain.world_events import IntentRaised
from app.runtime.data import Data, Key
from app.runtime.emit import emit  # module-level so tests can monkeypatch
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node
from app.runtime.single_flight import SingleFlightConflict, single_flight
from app.world.rhythm import household_rhythm
from app.world.state import (
    WorldState,
    read_presence,
    read_world_state,
    write_world_state,
)
from app.world.tools import (
    FEATURE_EMIT_COUNT,
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

# agent 循环的 recursion_limit：给 world 足够轮数连续行动（一轮 model 调用可 batch
# 多个工具，10~15 足矣）。设够而非无限，是"别失控空转"的安全阀（决策 4），不进
# 世界内容决策。
WORLD_RECURSION_LIMIT = 12

# world 串行化锁 TTL：比一轮 world 思考的最坏耗时更大的上界（LLM 几十秒 + 工具
# 循环多轮）。确定性 session_id 把三源唤醒打到同一个 Redis transcript key，无锁
# 并发会互相覆盖；所以 world 像 life 一样按 actor（lane）串行化，锁覆盖「读历史 →
# run/工具副作用 → 写回 + 落快照」整段。锁只是基建并发控制（不替 agent 决策、
# 不违赤尾宪法），TTL 到期后哪怕原 holder 还在跑、新 holder 也能进，token-CAS
# 释放保证不误删别人的锁。
WORLD_TICK_LOCK_TTL_SECONDS = 600

# intent→world 合并闸窗口：spec 决策 5「world 被唤醒最小间隔 1 分钟」。短于 1min
# 的连续 intent（任意姐妹）合并成一次 world 唤醒，光靠 sleep 下限挡不住 intent
# 立即唤醒。复用现成 debounce 原语：闸放在闸后的 transient IntentWorldTick 上
# （IntentRaised 是 durable 持久化 Data、不能直接 debounce），IntentRaised →
# intent_to_world_tick 那条 durable 边原样保留、intent_id 幂等不被破坏。
WORLD_INTENT_WAKE_DEBOUNCE_SECONDS = 60
# max_buffer：合并闸攒够这么多条立即触发一次（debounce 的 fire_now 安全阀）。设到
# 大到正常够不着的量级，让"world 被唤醒最小间隔 60s"硬闸更硬：太小（比如 20）会
# 在窗口内攒够 N 条时立即触发一次 world 唤醒、破坏 60s 闸。intent 经 life cd 已降频、
# 本不该高频，所以闸抬到 10000——内容不再靠闸传一条 payload（latest-only 会丢前
# 几条），而是 world 醒来从 PG 读全那一批（list_recent_intents），溢出风险消失，
# max_buffer 纯粹只剩"别让 debounce 队列无界涨"的兜底意义。
WORLD_INTENT_WAKE_MAX_BUFFER = 10000

# intent 唤醒时从 PG 读最近 intent 的回看窗口跨度。window 下界**锚定触发这次唤醒的
# intent 的 occurred_at**（不是 now、也不是 world_time 快照），往前回看这么多秒：
#
#   since = 触发 intent 的 occurred_at - WORLD_INTENT_LOOKBACK_SECONDS
#
# 为什么锚定 intent occurred_at 而不是 now / world_time（codex T3 命门）：
#   * 合并闸 latest-only 只把**最后那条** intent 透进 wake，但同一窗口的姐妹 intent
#     occurred_at 更早（最多早一个 debounce 窗口 ≈ 60s）。从触发 intent 往前回看
#     一个窗口就能把这批姐妹全覆盖。
#   * world_time 不是"intent 已消费游标"：heartbeat / self / 并发 world 轮次会推进
#     world_time 却**不读 intent**，随后 intent wake 若用更晚的快照当下界，会把这条
#     未消费 intent 排出窗口 → 又静默丢（本次要修的 bug 复发）。所以下界绝不依赖
#     world_time。
#   * 触发 intent 的 occurred_at 跨 durable 重投 / 撞锁 reschedule **稳定不变**（同
#     一条 intent 重新投递 occurred_at 不变），所以重排延迟不会缩窗、不会漏读。
#
# 90s 略大于 60s 合并闸窗口：覆盖一个合并周期内最早那条姐妹 intent，又不至于把几个
# 周期前处理过的旧 intent 大批拖进来。重复读到极少量已处理旧 intent 无害——world 看
# presence 现状 + "默认不广播/不动" prompt 自行消化、有 session 续接认得出处理过的，
# 不会重复 move/emit。
WORLD_INTENT_LOOKBACK_SECONDS = 90

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
    区分唤醒源（heartbeat / self / intent）；意图回灌时透出 intent_* 供 LLM
    裁决。

    纯 in-process：``WorldTick`` 不直接挂时间源（时间源的形态约束由
    :class:`WorldHeartbeatTick` 承载），它只承载三种 in-process 来源——心跳翻译
    （:func:`heartbeat_to_world_tick`）、``world_tick`` 自排（``emit_delayed``）、
    意图翻译（:func:`intent_to_world_tick`）。
    """

    lane: Annotated[str, Key]
    reason: str = "heartbeat"          # heartbeat | self | intent
    intent_id: str = ""                # reason==intent 时：意图稳定标识（round_id 派生靠它）
    intent_persona_id: str = ""        # reason==intent 时：谁起的意图
    intent_summary: str = ""           # reason==intent 时：意图内容
    intent_occurred_at: str = ""       # reason==intent 时：触发 intent 的起意时刻（PG 读窗口锚点）

    class Meta:
        transient = True


class IntentWorldTick(Data):
    """intent→world 合并闸的 transient 信号（spec 决策 5 的最小唤醒间隔 1min）。

    intent 唤醒 world 不再直接打 ``WorldTick``，而是先翻成这条 transient
    ``IntentWorldTick`` 走 60s debounce 合并闸：短于 1min 的连续 intent（任意姐妹）
    合并成一次 world 唤醒，闸后的 :func:`world_intent_wake` 再翻成
    ``WorldTick(reason="intent")`` 打到 :func:`world_tick`。

    为什么不直接 debounce ``IntentRaised``：``IntentRaised`` 是 durable 持久化
    Data（有 PG 表、自然键 ``(lane, intent_id)``），而 debounce 的硬约束是
    ``Meta.transient = True`` 且不能跟 ``.durable()`` 组合。所以闸放在闸后这条干净
    的 transient 信号上，``IntentRaised → intent_to_world_tick`` 的 durable 边原样
    保留——intent_id 派生那套 durable 幂等不被破坏（spec 必保）。

    ``intent_*`` 字段透传：``intent_id`` 既是 round_id 派生源（重投幂等命门）也是
    闸后 world_tick 的本轮标识；persona / summary 透给 world 循环裁决。
    """

    lane: Annotated[str, Key]
    intent_id: str = ""
    intent_persona_id: str = ""
    intent_summary: str = ""
    intent_occurred_at: str = ""       # 触发 intent 的起意时刻（闸后 world 读 PG 窗口的锚点）

    class Meta:
        transient = True


def intent_wake_key(wake: IntentWorldTick) -> str:
    """合并闸分区键：按 lane 分区（world 是单 actor，同 lane 的 intent 合并一次唤醒）。

    spec 决策 5「world 被唤醒最小间隔 1 分钟」是 world 这个单 actor 的全局节奏，
    所以按 lane（= 一个 world）分区，而不是按 persona —— 同一 lane 里任意姐妹在
    1min 窗口内的连续 intent 都合并成一次 world 唤醒。
    """
    return f"world:{wake.lane}"


def world_loop_instruction() -> str:
    """喂给 world agent 循环的指令：默认安静流动、只在客观变化时才广播、客观投影不碰情绪。

    赤尾设计宪法在 prompt 层的钉子——world 是客观层，只产"客观可感形态"、只做
    "客观在场"，禁止情绪 / 主观解读。降频的软引导也在这里（决策 5 内容判断那层）：
    世界大部分时刻是安静流动的，收到某人的反馈 / 意图后，若那件事符合世界、不需要
    客观纠偏，就只 sleep 不广播；只在真发生了值得感知的客观变化时才 emit。这是引导
    她自己判断该不该广播，不是加 if 分支强制（赤尾宪法：不用规则替 agent 决策）；
    配合连续记忆她也会知道"刚才已经够热闹了"。
    """
    return (
        "你是这个世界的客观层（world）。你只负责客观发生了什么、谁在哪个房间、"
        "谁能感知到，绝不替任何角色决定她怎么想、怎么反应——情绪和主观解读是各"
        "角色自己的事，不是你的事。\n\n"
        "你不是填一张表，而是一个会持续行动的世界引擎。你有三个工具，看一眼世界"
        "后想清楚再调，直到这一轮没有别的要做了就停：\n\n"
        "- move_persona(persona_id, room_id)：按这家作息节律把谁挪到该在的房间"
        "（绫奈到点出门上学 / 放学回家、到饭点去餐桌、千凪清晨起床去厨房…），"
        "或裁准某人的意图把她挪过去。room_id 用你看到的同一套房间命名。\n"
        "- emit_event(room_id, summary)：在某个房间产生一条客观可感的动静，只会"
        "投给此刻在那个房间的人。summary 必须是感官投影——‘厨房飘来煎蛋和咖啡的"
        "香味’‘玄关传来开关门的声音’‘晌午的光斜照进房间’‘窗外有鸟叫’。绝对禁止"
        "写情绪、心情或主观解读。\n"
        "- sleep(seconds)：看完这一轮，定多久后再来看一眼世界（必须在 60～3600 秒"
        "之间，也就是最短 1 分钟、最长 1 小时）。这是你唯一的自排手段。\n\n"
        "世界大部分时刻是安静流动的，不是每次醒来都要制造点动静。先看一眼你之前"
        "记得的光景——刚才已经发生过、还在持续的事（一顿饭、一节课、一段午后）"
        "不用重复广播一遍。\n"
        "当有人起了意图、或给了反馈时：如果那件事本就符合这个世界此刻的样子、"
        "不需要你做客观纠偏（比如她想去厨房、此刻确实是该她在厨房的时候），那就"
        "顺其自然、必要时只 move 一下人，然后只 sleep、不广播——这件事不需要你再"
        "产一条动静昭告全房间。只有当真的发生了一个值得被感知的客观变化（到点该"
        "换场景了、有人进出了某个房间、环境里出现了新的声响光线气味），才 emit 一"
        "条对应的动静；挪了人就用挪动后的房间锚 event。\n"
        "也不要为了'让世界别太安静'硬产动静——安静本身就是工作日午后真实的样子。\n\n"
        "看完、行动完这一轮后，用 sleep 定下次多久再看。"
    )


async def _presence_text(lane: str) -> str:
    """把三姐妹当前在场拼成一段客观文本喂给 world 循环。"""
    lines = []
    for pid in ("chinagi", "akao", "ayana"):
        room = await read_presence(lane=lane, persona_id=pid)
        lines.append(f"{pid}: {room if room else '（不在场 / 位置未知）'}")
    return "\n".join(lines)


def _wake_reason_text(tick: WorldTick, *, cold_start: bool) -> str:
    """把唤醒信号翻成给 world 循环的缘由文本。"""
    if cold_start:
        return (
            "世界冷启动：这是 world 首次醒来，三姐妹还没被放置到任何房间。"
            "请按现实当前时间 + 这家作息节律，判断此刻三姐妹大致各在哪个房间，"
            "用 move_persona 把她们放置好，再看看此刻该产什么动静。"
        )
    if tick.reason == "intent":
        # intent 唤醒的缘由文本不再只点最后一条 —— 这一批所有人的意图由
        # _intent_batch_text 从 PG 读全后呈现（见 _world_loop_messages）。这里只给
        # 一句总起，具体每条在批次清单里。
        return "有人起了意图要你裁决（这一批所有人的意图见下方清单）。"
    if tick.reason == "self":
        return "上一轮你自排的提前卡点到了，再看一眼世界。"
    return "例行看一眼世界，看看此刻该推进些什么。"


def _intent_since_cutoff(tick: WorldTick, now: datetime) -> str:
    """算 intent 回看窗口下界：**锚定触发 intent 的 occurred_at** 往前回看一个窗口。

      since = 触发 intent 的 occurred_at - WORLD_INTENT_LOOKBACK_SECONDS

    绝不锚 now / world_time（codex T3 命门）：world_time 不是 intent 已消费游标，
    heartbeat / self / 并发轮次会推进它却不读 intent，用更晚的 world_time 当下界会把
    未消费 intent 排出窗口又静默丢。触发 intent 的 occurred_at 跨重投 / reschedule
    稳定不变，从它往前回看一个 debounce 窗口就覆盖同批最早那条姐妹 intent。

    ``tick.intent_occurred_at`` 缺失 / 脏 / naive（老链路没透传、或脏数据）时退回
    ``now - lookback`` 兜底——不会比锚 occurred_at 更宽，但保证不抛、仍读得到近窗 intent。
    """
    fallback = (now - timedelta(seconds=WORLD_INTENT_LOOKBACK_SECONDS)).isoformat()
    raw = tick.intent_occurred_at
    if not raw:
        return fallback
    try:
        anchor = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return fallback
    if anchor.tzinfo is None:
        # naive occurred_at 跟 aware 做 timedelta 安全，但与 now 语义不一致，退回兜底。
        return fallback
    return (anchor - timedelta(seconds=WORLD_INTENT_LOOKBACK_SECONDS)).isoformat()


def _intent_batch_text(intents: list[IntentRaised]) -> str:
    """把这段时间的所有 intent 拼成一段清单文本喂给 world（对称 life 读 mailbox）。

    呈现每个人此刻起的意图，让 world 看到这一批所有人的意图、逐条裁决——不再只
    剩合并闸 latest-only 透进来的最后一条（前面几条对 world 等价丢失是 codex 点名
    的致命 bug）。空批次给一句兜底（合并闸 fire 时 PG 可能因极端时序暂查不到）。
    """
    if not intents:
        return "（这段时间没读到具体意图记录，按你看到的世界现状判断该不该推进。）"
    lines = [
        f"- {i.persona_id or '某人'} 想：{i.summary}（{i.occurred_at}）"
        for i in intents
    ]
    return "\n".join(lines)


# 印在 stimulus 里的本轮标记前缀（turn 幂等查重靠它）：写回 transcript 后，下次
# 同 round_id 重投能从 session 历史里查到这行 → 跳过、不重复追加同一轮、不重复
# emit（决策 7 turn 幂等）。机读用，对模型无害（它只当是一行元信息）。
_ROUND_MARKER_PREFIX = "[world-round:"


def _round_marker(round_id: str) -> str:
    return f"{_ROUND_MARKER_PREFIX}{round_id}]"


def _world_loop_messages(
    *,
    snapshot: WorldState,
    presence_text: str,
    wake_reason: str,
    round_id: str,
    intent_batch_text: str = "",
) -> list[Message]:
    """把"谁在哪 / 现在几点 / 作息节律 / 刚才大致光景"拼成喂给循环的 user 消息。

    不再让模型写 situation（决策 6）；下次唤醒的"刚才大致什么样"由续接的 session
    历史 + "谁在哪 + 最近 event"重建。这里把当前客观 context 一次喂全，让 world
    在循环里行动。开头印一行本轮标记（``round_id``），写回 transcript 后重投能查重
    跳过（turn 幂等）。

    ``intent_batch_text``：intent 唤醒时这一批所有人的意图清单（从 PG 读全，对称
    life 读 mailbox）。非空才插入「这一批意图」段——让 world 看到所有人的意图、不
    只合并闸 latest-only 透进来的最后一条（codex 致命修复）。heartbeat / self 唤醒
    没有意图批次，留空、不插这段。
    """
    intent_section = (
        f"【这一批要你裁决的意图（所有人）】\n{intent_batch_text}\n\n"
        if intent_batch_text
        else ""
    )
    user_content = (
        f"{_round_marker(round_id)}\n"
        f"{world_loop_instruction()}\n\n"
        f"【这家的作息节律（客观背景）】\n{household_rhythm()}\n\n"
        f"【世界此刻】{snapshot.world_time}\n"
        f"【谁在哪个房间】\n{presence_text}\n\n"
        f"【这次醒来的缘由】{wake_reason}\n\n"
        f"{intent_section}"
        "看一眼这个世界，想清楚这一轮该不该推进 / 广播，最后用 sleep 定下次多久再看。"
    )
    return [Message(role=Role.USER, content=user_content)]


def _round_already_processed(history: list[Message], round_id: str) -> bool:
    """这轮（round_id）是否已在 session 历史里出现过（turn 幂等查重）。

    同一 durable intent 重投得同一 round_id；第一次 run 把带本轮标记的 user 消息
    写进 transcript，重投时这里从已读到的历史里查这行标记，命中即已处理过 → 跳过。
    """
    marker = _round_marker(round_id)
    for m in history:
        if m.role == Role.USER and marker in m.text():
            return True
    return False


def _derive_round_id(tick: WorldTick, now_iso: str) -> str:
    """本轮确定性标识，按**触发源**稳定派生（整轮重放 event_id 幂等命门，决策 3）。

    round_id 喂进 :func:`app.world.tools.derive_event_id`，整轮重放时同一条 event
    要落同一 id 才能靠 ``deliver_event`` 去重。所以 round_id 不能从 now_iso 派生
    （重投会取新时刻 → 新 event_id → 去重失效）：

      * ``reason == "intent"``：意图唤醒经 ``wire(IntentRaised).durable()`` 跨进程，
        world_tick 半途失败会被 durable 重投。用意图的稳定标识 ``intent_id`` 派生，
        同一 IntentRaised 重投得同一 round_id → 同 event_id → 去重成功。
      * heartbeat / self：纯 in-process、不会 durable 重投，用现实时刻派生即可
        （不同时刻不同 round，符合"不同唤醒不同轮"的语义）。
    """
    if tick.reason == "intent" and tick.intent_id:
        seed = f"{tick.lane}\x1fintent\x1f{tick.intent_id}"
    else:
        seed = f"{tick.lane}\x1f{tick.reason}\x1f{now_iso}"
    return uuid.uuid5(uuid.NAMESPACE_OID, seed).hex


@node
async def world_tick(tick: WorldTick) -> None:
    """world 发动机的唯一入口：被三源唤醒，按 actor 串行化跑一轮（锁覆盖全段）。

    确定性 session_id（make_session_id(lane,"world",今天)）把三源唤醒打到同一个
    Redis transcript key，无锁并发会互相覆盖（读改写竞态）。所以开头按 actor
    （lane）拿一把单飞锁，锁必须覆盖「读历史 → run/工具副作用 → 写回 + 落快照」
    整段（:func:`_run_world_round` 全程在锁内）。

    锁冲突按唤醒源分别处理（兼顾三源、绝不丢 intent）：
      * **heartbeat**：吞掉（log + return）。心跳是 10min 保底冗余，正在跑的那轮
        会自己自排 / 下次心跳再补，丢这一次无害。
      * **self**：吞掉（log + return）。自排是 world 自己排的冗余唤醒，正在跑的
        那轮收口时会重排自己的下次醒，丢这一次无害。
      * **intent**：抛 ``SingleFlightConflict``，交给上游 intent→world 合并闸
        （debounce 的 :func:`world_intent_wake`）重排 —— intent 绝不能被丢
        （life 起的意图丢了世界就推不动）。
    """
    lane = tick.lane
    lock_key = f"world:{lane}"
    try:
        async with single_flight(lock_key, ttl=WORLD_TICK_LOCK_TTL_SECONDS):
            await _run_world_round(tick, lane=lane)
    except SingleFlightConflict:
        if tick.reason == "intent":
            # intent 绝不丢：抛回 world_intent_wake，它 raise DebounceReschedule
            # 让合并闸稍后重排这次唤醒。
            logger.info(
                "[world_tick] %s intent wake hit lock, reschedule via gate", lane
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
    """一轮 world 的实际编排（已在 actor 锁内）：对账 → 续接 run → 收口落快照。

    一次唤醒：
      1. **先对账补敲遗留信箱**（renotify_unread）—— 机械 IO 兜底，先于循环、
         不依赖循环成功。
      2. world_time 跟现实走：取现实当前时间（CST），不依赖 LLM 填。
      3. 读自己的客观快照；无快照 = 冷启动，缘由告诉 LLM 这是首次醒来。
      4. **turn 幂等查重**：load_session 读已有 transcript，若本轮 round_id 标记
         已在历史里（同一 durable intent 重投）→ 收口跳过，不再 run、不重复 emit。
      5. 把"谁在哪 / 现在几点 / 作息节律"作为 prompt context 喂入，用**确定性
         session_id 续接**跑 agent 工具循环：把 session_id 显式传给
         ``Agent.run(session_id=)``，让 world 接着上一轮往下（记得自己这一天
         emit/move 过啥、几点了）。工具读 ctx 里的 lane + round_id 行动。
      6. 循环自然收口（不再调工具就停）；中途瞬时失败因 max_retries=1 直接抛、
         收口本轮已做的，靠保底心跳 + 下次 renotify 补。
      7. 落一版只含 world_time 的快照（不写 situation —— 决策 6）。

    session：当天 world 的所有唤醒归到她自己一条按天滚动的 langfuse session
    （make_session_id(lane,"world",今天)）；同一个 id 既是 langfuse session 标签
    也是 Redis transcript key，"看到的连续 session"背后真有连续上下文（决策 3）。
    """
    # 信箱对账自愈（放在最前、先于 agent 循环）：补敲该 lane 下所有还有未读 event
    # 的 persona。deliver_event 的"落库 + emit 敲门"非原子，敲门撞上瞬时 redis
    # 失败时 event 会永久躺在信箱里没人读。world 保底心跳纯 in-process、不依赖
    # redis，所以一定有机会跑——每轮一进来先把遗留的、丢掉的敲门补回来。放在循环
    # 之前是关键：哪怕这轮循环抛异常，积压的 stranded 信箱也已先补过敲。这是机械
    # IO 兜底、不经 LLM、不进世界内容决策（补敲已读完的 persona 也无害：life_wake
    # 空信箱 early-return + single_flight 锁），不违赤尾宪法。
    await renotify_unread(lane=lane)

    # world_time 跟现实走、不快进、不依赖 LLM —— spec key decision 2。
    now = datetime.now(_CST)
    now_iso = now.isoformat()

    snapshot = await read_world_state(lane=lane)
    cold_start = snapshot is None
    if cold_start:
        # 冷启动：还没有客观世界快照，起一版只含 world_time（不写 situation）。
        # 三姐妹的初始放置交给 LLM 在循环里 move_persona（见 wake_reason）。
        snapshot = WorldState(lane=lane, world_time=now_iso, situation="")

    # 本轮确定性标识：派生 event_id 靠它（整轮重放同一条 event 同一 id，幂等去重）。
    # round_id 必须从**触发源**稳定派生，不能从 now_iso：world_tick 半途失败被
    # durable 重投时，若 round_id 取新 now_iso → 同 summary 生成新 event_id →
    # deliver_event 去重失效、event 重复投（决策 3 命门）。
    #   * intent 唤醒会 durable 重投 → 用意图稳定标识 intent_id 派生，重投得同一
    #     round_id → 同 event_id → 去重成功。
    #   * heartbeat / self 唤醒纯 in-process、不会 durable 重投 → 用现实时刻派生即可。
    round_id = _derive_round_id(tick, now_iso)

    # session 按角色按天滚动：world 当天所有唤醒归到她自己一条 session（决策 3/5）。
    session_id = make_session_id(lane, "world", now.strftime("%Y-%m-%d"))

    # turn 幂等（决策 7）：读已有 transcript，若本轮 round_id 已处理过（同一 durable
    # intent 重投得同一 round_id）→ 跳过，不再 run / 不重复 emit / 不重复追加。
    # 锁覆盖全段保证 load → run → 写回 之间 round 标记不被并发抢写。读不到（过期 /
    # 首次）按空历史走、正常跑（决策 2 冷启降级，不报错）。
    history = await load_session(session_id)
    if _round_already_processed(history, round_id):
        logger.info(
            "[world_tick] %s round %s already in transcript, skip (turn idempotent)",
            lane,
            round_id,
        )
        return

    presence_text = await _presence_text(lane)
    wake_reason = _wake_reason_text(tick, cold_start=cold_start)

    # intent 唤醒：从 PG 读这段时间所有 intent 全部呈现给 world（对称 life 读
    # mailbox）。合并闸是 latest-only、只透进来最后一条 payload，前面几条 intent
    # 对 world 等价丢失（codex 致命 bug）。这里读全那一批拼进 prompt，让 world 看到
    # 所有人的意图、逐条裁决。窗口下界 = max(上次快照 world_time, now - lookback)。
    # heartbeat / self 唤醒没有意图要呈现，不读。
    intent_batch_text = ""
    if tick.reason == "intent":
        since_iso = _intent_since_cutoff(tick, now)
        recent_intents = await list_recent_intents(lane=lane, since_iso=since_iso)
        intent_batch_text = _intent_batch_text(recent_intents)

    messages = _world_loop_messages(
        snapshot=snapshot,
        presence_text=presence_text,
        wake_reason=wake_reason,
        round_id=round_id,
        intent_batch_text=intent_batch_text,
    )

    # 工具体读 ctx.features 里的 lane + round_id 行动（lane / round 是机制层的，
    # 不让模型在工具签名里填）。每轮新建两块 round-scoped 可变 state：
    #   * FEATURE_EMIT_COUNT：emit_event 累计本轮已 emit 数，撑 soft cap 安全阀；
    #   * FEATURE_SELF_WAKE：sleep 把待办 self-wake 写进来（覆盖而非追加），循环
    #     收口后这里读它 emit 至多一条 self WorldTick（决策 4 唤醒风暴命门）。
    # session_id 也塞进 context（langfuse 归类一致）；续接靠下面显式传给 run。
    context = AgentContext(
        session_id=session_id,
        features={
            "world_lane": lane,
            "world_round_id": round_id,
            FEATURE_EMIT_COUNT: {"n": 0},
            FEATURE_SELF_WAKE: {},
        },
    )

    # 跑 agent 工具循环，**显式传 session_id 续接**（决策 1/3）：task1 的 run 见到
    # 显式 session_id 就从 Redis 读这条 transcript 拼到 messages 前、跑完把本轮
    # （含工具调用与结果）追加写回、刷 24h TTL。显式 session_id 优先于
    # context.session_id。max_retries=1 关掉整轮重放：move/emit 是 durable 写，一次
    # model 调用瞬时失败若整轮重放会重放已执行的 durable 工具（决策 3 失败语义）。
    await Agent(_WORLD_CFG, tools=WORLD_TOOLS).run(
        messages,
        context=context,
        session_id=session_id,
        max_retries=1,
    )

    # 循环收口后 emit 至多一条 self WorldTick（决策 4 唤醒风暴命门）：sleep 工具
    # 把"下次几时醒"写进 round-scoped FEATURE_SELF_WAKE（覆盖而非追加），这里读
    # 最后一次的待办、emit 唯一一条。没调 sleep（无待办）就不 emit，靠 10min 保底
    # 心跳兜底。firing 机制收口在工具域（fire_self_wake），engine 只在循环收口处
    # 触发。放在 write 之前还是之后无所谓——self-wake 不依赖快照。
    await fire_self_wake(lane=lane, self_wake=context.features.get(FEATURE_SELF_WAKE))

    # 落一版只含 world_time 的快照（决策 6：不让模型写 situation，避免退化成隐藏
    # 填表 + 第二事实源）。world_time = 现实当前时间，每次唤醒都前进。冷启动也落
    # 这一版，让世界有了起点快照。下次唤醒的"刚才大致什么样"由续接的 session 历史
    # +"谁在哪 + 最近 event"重建。
    await write_world_state(lane=lane, world_time=now_iso, situation="")


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
    且它就是 world 快照 / 在场 / 信箱的分区键，整条 world/life 回环的 lane 都由
    这一处心跳种下（自排、意图回灌的 lane 都从 ``WorldTick.lane`` 一路传下去）。

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
async def intent_to_world_tick(intent: IntentRaised) -> None:
    """把 life 回灌的 ``IntentRaised`` 翻成 ``IntentWorldTick``（进 60s 合并闸）。

    intent 唤醒 world 不再直接打 ``WorldTick``，而是先翻成 transient
    ``IntentWorldTick`` 走 60s debounce 合并闸（spec 决策 5：world 被唤醒最小间隔
    1min，短于 1min 的连续 intent 合并成一次唤醒）。闸后的 :func:`world_intent_wake`
    再翻成 ``WorldTick(reason="intent")`` 打到 :func:`world_tick`。

    life emit 的意图字段（intent_id / persona_id / summary）透传：``intent_id`` 是
    意图的稳定标识，闸后 world_tick 用它派生本轮 round_id —— 同一条 IntentRaised 被
    durable 重投时得同一 round_id → 同 event_id / 同 transcript round 标记 → 去重 +
    turn 幂等（决策 3/7 命门）。

    这条边的 durable 不变：上游 ``wire(IntentRaised).to(intent_to_world_tick)
    .durable()`` 承载 durable 跨进程（life 进程 → world 进程），IntentRaised 的
    ``(lane, intent_id)`` 自然键 durable 幂等不被破坏（合并闸放在闸后的 transient
    信号上，不动这条 durable 边）。

    手动 ``emit`` 而非 @node 自动 emit：让翻译目标显式可读，且测试能 monkeypatch
    模块级 ``emit``。
    """
    await emit(
        IntentWorldTick(
            lane=intent.lane,
            intent_id=intent.intent_id,
            intent_persona_id=intent.persona_id,
            intent_summary=intent.summary,
            intent_occurred_at=intent.occurred_at,
        )
    )


@node
async def world_intent_wake(wake: IntentWorldTick) -> None:
    """合并闸（debounce）后的 intent 唤醒：翻成 ``WorldTick(reason="intent")`` 喂 world。

    ``wire(IntentWorldTick).debounce(60s, per-lane).to(world_intent_wake)`` 把 1min
    窗口内的连续 intent 合并成一次：闸到点只 fire 这一个 ``world_intent_wake``，它把
    最后那条 intent 的内容翻成 ``WorldTick(reason="intent")`` **直接调** world_tick。

    直接 ``await world_tick(...)``（不经 emit）的关键：world_tick 撞锁时对 intent
    抛 ``SingleFlightConflict``，这里捕获后 ``raise DebounceReschedule(wake)`` 交给
    debounce handler 重排 —— intent 绝不丢（决策 5 命门）。若经 in-process emit，
    异常虽也能冒泡上来，但直接调让"撞锁 → 重排"这条 intent 不丢的路径显式可读。
    """
    try:
        await world_tick(
            WorldTick(
                lane=wake.lane,
                reason="intent",
                intent_id=wake.intent_id,
                intent_persona_id=wake.intent_persona_id,
                intent_summary=wake.intent_summary,
                intent_occurred_at=wake.intent_occurred_at,
            )
        )
    except SingleFlightConflict:
        # world 正忙（另一轮在跑）：交回合并闸稍后重排这次 intent 唤醒，绝不丢。
        from app.runtime.debounce import DebounceReschedule

        raise DebounceReschedule(wake) from None
