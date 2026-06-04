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
from app.agent.trace import make_session_id
from app.data.queries.mailbox import renotify_unread
from app.domain.world_events import IntentRaised
from app.runtime.data import Data, Key
from app.runtime.emit import emit  # module-level so tests can monkeypatch
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node
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

    class Meta:
        transient = True


def world_loop_instruction() -> str:
    """喂给 world agent 循环的指令：用工具主动按节律推进世界、客观投影不碰情绪。

    赤尾设计宪法在 prompt 层的钉子——world 是客观层，只产"客观可感形态"、只做
    "客观在场"，禁止情绪 / 主观解读。这是 agent 工具指令（不是填表指令）：告诉她
    她有 move_persona / emit_event / sleep 三个工具，主动按这家作息节律推进世界，
    平淡时段也把生活质感 emit 出来，宁可多产平淡也别让世界死寂。
    """
    return (
        "你是这个世界的客观层（world）。你只负责客观发生了什么、谁在哪个房间、"
        "谁能感知到，绝不替任何角色决定她怎么想、怎么反应——情绪和主观解读是各"
        "角色自己的事，不是你的事。\n\n"
        "你不是填一张表，而是一个会持续行动的世界引擎。你有三个工具，看一眼世界"
        "后连续调用它们来推进世界，想清楚了再调，直到这一轮没有别的要做了就停："
        "\n\n"
        "- move_persona(persona_id, room_id)：按这家作息节律把谁挪到该在的房间"
        "（绫奈到点出门上学 / 放学回家、到饭点去餐桌、千凪清晨起床去厨房…），"
        "或裁准某人的意图把她挪过去。room_id 用你看到的同一套房间命名。\n"
        "- emit_event(room_id, summary)：在某个房间产生一条客观可感的动静，只会"
        "投给此刻在那个房间的人。summary 必须是感官投影——‘厨房飘来煎蛋和咖啡的"
        "香味’‘玄关传来开关门的声音’‘晌午的光斜照进房间’‘窗外有鸟叫’。绝对禁止"
        "写情绪、心情或主观解读。\n"
        "- sleep(seconds)：看完这一轮，定多久后再来看一眼世界（必须 ≤ 3600 秒，"
        "也就是最长 1 小时）。这是你唯一的自排手段。\n\n"
        "世界是持续活的，不是只有大事才值得记。平淡的工作日下午也有午后的光、"
        "厨房的水声、窗外的车响、楼下巷子的人声——把这些客观可感的生活质感主动"
        "emit 出来。宁可多产几条平淡的动静，也别让世界陷入死寂。挪了人就用挪动后"
        "的房间锚 event（把绫奈挪进玄关、就在玄关产‘开关门的声音’）。\n\n"
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
    if tick.reason == "intent" and tick.intent_summary:
        who = tick.intent_persona_id or "某人"
        return f"{who} 起了一个意图要你裁决：{tick.intent_summary}"
    if tick.reason == "self":
        return "上一轮你自排的提前卡点到了，再看一眼世界。"
    return "例行看一眼世界，看看此刻该推进些什么。"


def _world_loop_messages(
    *, snapshot: WorldState, presence_text: str, wake_reason: str
) -> list[Message]:
    """把"谁在哪 / 现在几点 / 作息节律 / 刚才大致光景"拼成喂给循环的 user 消息。

    不再让模型写 situation（决策 6）；下次唤醒的"刚才大致什么样"由"谁在哪 +
    最近 event"重建。这里把当前客观 context 一次喂全，让 world 在循环里行动。
    """
    user_content = (
        f"{world_loop_instruction()}\n\n"
        f"【这家的作息节律（客观背景）】\n{household_rhythm()}\n\n"
        f"【世界此刻】{snapshot.world_time}\n"
        f"【谁在哪个房间】\n{presence_text}\n\n"
        f"【这次醒来的缘由】{wake_reason}\n\n"
        "看一眼这个世界，连续调用工具推进它，最后用 sleep 定下次多久再看。"
    )
    return [Message(role=Role.USER, content=user_content)]


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
    """world 发动机的唯一入口：被三源唤醒 → 对账补敲遗留信箱 → 跑 agent 工具循环 → 落快照。

    一次唤醒：
      1. **先对账补敲遗留信箱**（renotify_unread）—— 机械 IO 兜底，先于循环、
         不依赖循环成功。
      2. world_time 跟现实走：取现实当前时间（CST），不依赖 LLM 填。
      3. 读自己的客观快照；无快照 = 冷启动，缘由告诉 LLM 这是首次醒来。
      4. 把"谁在哪 / 现在几点 / 作息节律 / 刚才大致光景"作为 prompt context 喂入，
         跑 agent 工具循环：world 在循环里连续调 move_persona / emit_event / sleep
         推进世界（平淡时段也主动产 event）。工具读 ctx 里的 lane + round_id 行动。
      5. 循环自然收口（不再调工具就停）；中途瞬时失败因 max_retries=1 直接抛、
         收口本轮已做的，靠保底心跳 + 下次 renotify 补。
      6. 落一版只含 world_time 的快照（不写 situation —— 决策 6）。

    session：当天 world 的所有唤醒归到她自己一条按天滚动的 langfuse session
    （make_session_id(lane, "world", 今天)），能连续看 world 一天的意识流。
    """
    lane = tick.lane

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

    presence_text = await _presence_text(lane)
    wake_reason = _wake_reason_text(tick, cold_start=cold_start)
    messages = _world_loop_messages(
        snapshot=snapshot, presence_text=presence_text, wake_reason=wake_reason
    )

    # 本轮确定性标识：派生 event_id 靠它（整轮重放同一条 event 同一 id，幂等去重）。
    # round_id 必须从**触发源**稳定派生，不能从 now_iso：world_tick 半途失败被
    # durable 重投时，若 round_id 取新 now_iso → 同 summary 生成新 event_id →
    # deliver_event 去重失效、event 重复投（决策 3 命门）。
    #   * intent 唤醒会 durable 重投 → 用意图稳定标识 intent_id 派生，重投得同一
    #     round_id → 同 event_id → 去重成功。
    #   * heartbeat / self 唤醒纯 in-process、不会 durable 重投 → 用现实时刻派生即可。
    round_id = _derive_round_id(tick, now_iso)

    # session 按角色按天滚动：world 当天所有唤醒归到她自己一条 session（决策 5）。
    session_id = make_session_id(lane, "world", now.strftime("%Y-%m-%d"))

    # 工具体读 ctx.features 里的 lane + round_id 行动（lane / round 是机制层的，
    # 不让模型在工具签名里填）。每轮新建两块 round-scoped 可变 state：
    #   * FEATURE_EMIT_COUNT：emit_event 累计本轮已 emit 数，撑 soft cap 安全阀；
    #   * FEATURE_SELF_WAKE：sleep 把待办 self-wake 写进来（覆盖而非追加），循环
    #     收口后这里读它 emit 至多一条 self WorldTick（决策 4 唤醒风暴命门）。
    context = AgentContext(
        session_id=session_id,
        features={
            "world_lane": lane,
            "world_round_id": round_id,
            FEATURE_EMIT_COUNT: {"n": 0},
            FEATURE_SELF_WAKE: {},
        },
    )

    # 跑 agent 工具循环。max_retries=1 关掉整轮重放：move/emit 是 durable 写，一次
    # model 调用瞬时失败若整轮重放会重放已执行的 durable 工具（决策 3 失败语义）。
    # 中途失败就让它抛、收口本轮已做的，靠保底心跳 + 下次 renotify 补。
    await Agent(_WORLD_CFG, tools=WORLD_TOOLS).run(
        messages,
        context=context,
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
    # 这一版，让世界有了起点快照。下次唤醒的"刚才大致什么样"由"谁在哪 + 最近
    # event"重建。
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
    """把 life 回灌的 ``IntentRaised`` 翻成 ``WorldTick(reason="intent")``。

    life emit 的意图字段（intent_id / persona_id / summary）翻进 WorldTick 的
    intent_*，让 world 被 reason="intent" 唤醒、把意图内容透给 LLM 裁决。
    ``intent_id`` 是意图的稳定标识，world_tick 用它派生本轮 round_id —— 同一条
    IntentRaised 被 durable 重投时得同一 round_id → 同 event_id → 去重幂等
    （决策 3 命门）。这是 life → world
    的回灌边的"变速箱"：上游 ``wire(IntentRaised).to(intent_to_world_tick)
    .durable()`` 承载 durable 跨进程；下游 ``WorldTick`` 经 in-process 边打到
    ``world_tick``。

    手动 ``emit`` 而非 @node 自动 emit：让翻译目标显式可读，且测试能 monkeypatch
    模块级 ``emit``。
    """
    await emit(
        WorldTick(
            lane=intent.lane,
            reason="intent",
            intent_id=intent.intent_id,
            intent_persona_id=intent.persona_id,
            intent_summary=intent.summary,
        )
    )
