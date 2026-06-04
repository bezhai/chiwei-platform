"""world engine 节点 — Task 2 + stage3 联调收口.

world 是这个世界的发动机。它被**三源唤醒**，每次唤醒走同一条回路：world_time
跟现实走 → 读自己的客观快照（无快照=冷启动，让 LLM 按现实时间+节律放置三姐妹）
→ 让 LLM 推演此刻该不该挪人（presence_changes）、有没有"够格成为 event"的事、
锚在哪个房间 → 先应用在场变更（set_presence）再按所锚房间的当前在场集合投递
（客观感官投影、绝不含情绪）→ 落最新快照 → 自排下次醒。

世界"动起来"靠两条驱动：① 节律驱动（到点该上学/放学/吃饭，world 按客观边界把
人挪到对应房间并产对应 ambient event）；② 意图裁决驱动（reason="intent" 时
life 说"我想去厨房"，world LLM 判断合理就挪人并产 event）。谁移动、移动到哪、
产不产 event、裁不裁准意图——全由 LLM 判断，代码只忠实落它的决定。

三个唤醒源（都打到 ``world_tick`` 节点，靠 :class:`WorldTick` 的 ``reason``
区分）：

  1. **保底心跳**：``Source.interval(WORLD_HEARTBEAT_SECONDS)`` 每 10 分钟喂一条
     单字段 :class:`WorldHeartbeatTick`（满足框架时间源的单字段 ts 约定），由
     :func:`heartbeat_to_world_tick` 翻成 ``WorldTick(reason="heartbeat")`` 踹
     world 一下（世界时钟的滴答，只叫它看一眼）。这钉死最长停摆——所有 life
     都靠 world 启动，world 睡死世界就死。时间源不直接喂 ``WorldTick``：那会在
     源循环 ``_build_payload(WorldTick(ts=...))`` 处 ValidationError 杀 Pod
     （``WorldTick`` 无 ts、缺必填 lane），world 在生产里永远起不来。
  2. **自排提前卡点**：world 处理完决定下次几时醒，``emit_delayed`` 一个
     WorldTick，**只能在 10 分钟保底心跳内提前**（``WORLD_MAX_SELF_WAKE_MS
     == WORLD_HEARTBEAT_MS``）。绝不许排长闹钟（早 6 点排到下午没人能踹它）。
  3. **life 回灌的意图**：life emit ``IntentRaised`` → :func:`intent_to_world_tick`
     翻成 ``WorldTick(reason="intent")`` 打到这个节点，world 被唤醒去裁决。

赤尾设计宪法（硬约束）：
  * "够不够格成 event""谁该感知""客观世界变没变"全由 LLM 判断——代码里没有
    任何阈值 / 计数器 / 随机池 / if 分支替它决策。10 分钟心跳 / 自排只决定
    "何时醒"，绝不进入世界内容的决策。
  * world 只做"客观事实 → 各位置客观可感形态"的感官投影，**绝不碰情绪 / 主观
    解读**（那是 life 的事）。这条由喂 LLM 的 :func:`world_deliberation_instruction`
    在 prompt 层钉死。
  * 信息差产生侧过滤：event 锚定房间，只投给该房间当前在场的 persona（不为不
    在场的人产 event）。

框架原语：``Source.interval`` 心跳、``emit_delayed`` 自排、``deliver_event``
投递（Task 1，按 room_id + 在场集合）、``insert_append`` / ``select_latest``
快照（经 ``app.world.state``）。本节点只用现成原语，不改 runtime。

wiring（interval 心跳源 → WorldHeartbeatTick → heartbeat_to_world_tick；
IntentRaised → intent_to_world_tick；WorldTick 纯 in-process 接回 world_tick）
在 ``app/wiring/life_dataflow.py`` 收口。本模块提供节点 + domain + LLM 推演 +
两个翻译节点。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import BaseModel

from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.data.queries.mailbox import deliver_event
from app.domain.world_events import EVENT_KIND_AMBIENT, IntentRaised
from app.runtime.data import Data, Key
from app.runtime.emit import emit, emit_delayed  # module-level so tests can monkeypatch
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node
from app.world.rhythm import household_rhythm
from app.world.state import (
    WorldState,
    personas_in_room,
    read_presence,
    read_world_state,
    set_presence,
    write_world_state,
)

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

# 保底心跳：world 最长不睡过 10 分钟。这钉死最长停摆——所有 life 都靠 world
# 启动，world 睡死世界就死。自排只能在这个窗口内提前卡点。
WORLD_HEARTBEAT_SECONDS = 600
WORLD_HEARTBEAT_MS = WORLD_HEARTBEAT_SECONDS * 1000
# 自排上限 == 保底心跳：world 不许排长闹钟。
WORLD_MAX_SELF_WAKE_MS = WORLD_HEARTBEAT_MS

_WORLD_CFG = AgentConfig("world_deliberate", "offline-model", "world-deliberate")


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
    intent_persona_id: str = ""        # reason==intent 时：谁起的意图
    intent_summary: str = ""           # reason==intent 时：意图内容

    class Meta:
        transient = True


class WorldEventDraft(BaseModel):
    """LLM 推演产出的一条 event 草案（客观感官投影）。

    ``room_id`` 是 event 锚定的房间——world 按此房间的当前在场集合投递。
    ``summary`` 是客观可感形态的文字（"飘来饭菜香"），绝不含情绪 / 解读。
    """

    room_id: str
    summary: str
    occurred_at: str


class PresenceChange(BaseModel):
    """LLM 推演产出的一条在场变更：某 persona 此刻挪到某房间。

    这是世界"动起来"的核心——节律驱动（到点上学 / 放学 / 吃饭，world 按客观
    边界把人挪到对应房间）和意图裁决驱动（life 说"我想去厨房"、world 判断合理
    就挪人）两条路径都用这同一张表达。``room_id`` 与 event 锚定房间复用同一套
    命名（LLM 看得到当前在场分布文本，引导它复用）。
    """

    persona_id: str
    room_id: str


class WorldDeliberation(BaseModel):
    """LLM 一轮推演的结构化产出。

    ``presence_changes`` 为空 = 没人挪动；非空 = world 判断该把某些人挪到新
    房间（节律到点 / 意图裁准）。``events`` 为空 = world 克制不产 event（大部分
    心跳如此）。``next_situation`` 让 world 落最新客观情形文字（world_time 不由
    LLM 决定——它跟现实走，见 :func:`world_tick`）。

    BaseModel（非 durable Data），所以可以用 list 字段表达多条变更，不撞
    framework persist 层的 JSONB gap。
    """

    presence_changes: list[PresenceChange] = []
    events: list[WorldEventDraft] = []
    next_situation: str = ""
    # 可选的提前卡点需求：world LLM 觉得"过会儿得再看一眼"（饭快好了想 5 分钟后
    # 看一眼）时，填一个"几秒后再看"的秒数；不需要提前看就留 0。只有 0 < x 时
    # world_tick 才 emit 一条 self WorldTick（夹到 ≤ 保底心跳），否则靠 interval
    # 保底心跳兜底、不自排——避免每轮无条件自排在源循环下线性累积（必改 1）。
    # 提前与否由 LLM 判断（赤尾宪法：代码不设阈值 / 定时替它决策）。
    next_check_seconds: int = 0
    # 历史兼容字段：world_time 现在跟现实走、不读这个；保留只为不破坏旧 LLM 输出
    # 解析（LLM 多填一个字段无害）。world_tick 不再读它。
    next_world_time: str = ""


def world_deliberation_instruction() -> str:
    """喂给 LLM 的推演指令：客观感官投影 + 在场变更 + 克制 + 绝不碰情绪。

    赤尾设计宪法在 prompt 层的钉子——world 只产"客观可感形态"、只做"客观在场"，
    禁止情绪 / 主观解读，且大部分时候克制不产 event。同时告诉 LLM 它能/该表达
    在场变更（到点按节律挪人、裁准意图挪人）。
    """
    return (
        "你是这个世界的客观层（world）。你只描述客观发生了什么、谁在哪个房间、"
        "谁能感知到，绝不替任何角色决定她怎么想、怎么反应。\n\n"
        "你能做两件客观的事：\n\n"
        "【一、维护在场（presence_changes）】\n"
        "你看得到三姐妹此刻各在哪个房间。结合这家的作息节律和当前缘由，判断此刻"
        "该不该把谁挪到别的房间：\n"
        "- 节律到点（绫奈该出门上学 / 放学回家、到饭点该去餐桌、千凪起床去厨房…）"
        "这类有客观边界的事，把相关 persona 挪到对应房间；\n"
        "- 有人起了意图要去某处（缘由里会写），你判断合理就把她挪过去。\n"
        "用 presence_changes 表达：每条 = 某 persona_id 挪到某 room_id。没人需要"
        "挪动就留空。room_id 用你在‘谁在哪个房间’里看到的同一套房间命名"
        "（kitchen / 各自房间 / 玄关 / 客厅 / 餐桌 / 学校…）。\n\n"
        "【二、产客观 event（events）】\n"
        "判断此刻客观世界有没有‘够格成为 event’的事。绝大多数时刻是平淡的，"
        "不值得惊动谁——这时就别产 event。只有真的发生了客观可感的变化时才产。\n"
        "产出的每条 event：\n"
        "- 锚定到它发生的房间（room_id），world 只会投给当时在那个房间的人。挪了"
        "人就用挪动后的房间锚 event（比如把绫奈挪进玄关、就在玄关产‘开关门的声音’）；\n"
        "- summary 必须是‘客观可感的形态’——是感官投影，比如‘厨房飘来煎蛋和"
        "咖啡的香味’‘玄关传来开关门的声音’‘晌午的光照进房间’。\n"
        "- 绝对禁止写情绪、主观解读或谁的心情（不要写‘千凪在疼你们’‘她很开心’"
        "这类）。那是各角色自己的事，不是你的事。\n\n"
        "【三、要不要过会儿再看一眼（next_check_seconds）】\n"
        "你默认每 10 分钟会被叫醒看一眼世界。如果此刻有件事接下来很快会变化、值得"
        "提前再看（比如饭快好了、想几分钟后看看出锅没；有人快到家了），填一个"
        "next_check_seconds = 几秒后再看（必须小于 600 秒，也就是 10 分钟内提前）。"
        "没有这种需要提前看的事就填 0，靠默认的 10 分钟节拍兜底。这只决定‘下次几时"
        "再看一眼’，不进世界内容的决策。"
    )


async def _world_deliberate(
    *,
    lane: str,
    snapshot: WorldState,
    presence_text: str,
    wake_reason: str,
) -> WorldDeliberation:
    """让 LLM 读客观快照 + 在场 + 节律，判断此刻产不产 event、产什么。

    用 structured output（``Agent.extract``）拿回 :class:`WorldDeliberation`。
    "够不够格成 event""谁该感知"全由 LLM 决定，代码不设阈值。测试 mock 本函数。
    """
    user_content = (
        f"{world_deliberation_instruction()}\n\n"
        f"【这家的作息节律（客观背景）】\n{household_rhythm()}\n\n"
        f"【世界此刻】{snapshot.world_time}\n"
        f"【当前客观情形】{snapshot.situation}\n"
        f"【谁在哪个房间】\n{presence_text}\n\n"
        f"【这次醒来的缘由】{wake_reason}\n\n"
        "看一眼这个世界，判断此刻有没有够格成为 event 的客观变化。"
    )
    result = await Agent(_WORLD_CFG, update_trace=False).extract(
        WorldDeliberation,
        messages=[Message(role=Role.USER, content=user_content)],
    )
    return result  # type: ignore[return-value]


async def _presence_text(lane: str) -> str:
    """把三姐妹当前在场拼成一段客观文本喂给 LLM。"""
    lines = []
    for pid in ("chinagi", "akao", "ayana"):
        room = await read_presence(lane=lane, persona_id=pid)
        lines.append(f"{pid}: {room if room else '（不在场 / 位置未知）'}")
    return "\n".join(lines)


def _wake_reason_text(tick: WorldTick, *, cold_start: bool) -> str:
    """把唤醒信号翻成给 LLM 的缘由文本。"""
    if cold_start:
        return (
            "世界冷启动：这是 world 首次醒来，三姐妹还没被放置到任何房间。"
            "请按现实当前时间 + 这家作息节律，判断此刻三姐妹大致各在哪个房间，"
            "用 presence_changes 把她们放置好。"
        )
    if tick.reason == "intent" and tick.intent_summary:
        who = tick.intent_persona_id or "某人"
        return f"{who} 起了一个意图要裁决：{tick.intent_summary}"
    if tick.reason == "self":
        return "上一轮自排的提前卡点到了，再看一眼世界。"
    return "保底心跳：例行看一眼世界。"


@node
async def world_tick(tick: WorldTick) -> None:
    """world 发动机的唯一入口：被三源唤醒 → 推演 → 改在场 → 投递 → 自排下次醒。

    一次唤醒：
      1. world_time 跟现实走：取现实当前时间（CST），不依赖 LLM 填。
      2. 读自己的客观快照；无快照 = 冷启动，缘由告诉 LLM 这是首次醒来、要按现实
         时间 + 节律把三姐妹放置到各自此刻该在的房间（不硬编逐时刻死表）。
      3. 让 LLM 推演：此刻该不该挪人（presence_changes）、产不产 event（克制是常态）。
      4. **先应用在场变更（set_presence），再按房间在场集合投递**——顺序命门：
         刚挪进某房间的人，要能收到锚到该房间的 event。
      5. 对每条 event：按所锚房间的当前在场集合投递（不在场的收不到）。
      6. 落最新世界快照（world_time = 现实当前时间；situation 由 LLM 推进）。
      7. 自排下次醒（≤ 10 分钟保底心跳，world 不许排长闹钟）。
    """
    lane = tick.lane

    # world_time 跟现实走、不快进、不依赖 LLM —— spec key decision 2。
    now_iso = datetime.now(_CST).isoformat()

    snapshot = await read_world_state(lane=lane)
    cold_start = snapshot is None
    if cold_start:
        # 冷启动：还没有客观世界快照，起一版。起手 situation 只铺"现在大致是什么
        # 光景"，不下命令、不碰情绪。三姐妹的初始放置交给 LLM 推演（见 wake_reason）。
        snapshot = WorldState(
            lane=lane,
            world_time=now_iso,
            situation="（世界刚起手，还没有人被放置。）",
        )

    presence_text = await _presence_text(lane)
    wake_reason = _wake_reason_text(tick, cold_start=cold_start)

    deliberation = await _world_deliberate(
        lane=lane,
        snapshot=snapshot,
        presence_text=presence_text,
        wake_reason=wake_reason,
    )

    # 先应用在场变更，再投递（顺序命门：刚挪进房间的人要收得到锚该房间的 event）。
    # 谁移动、移动到哪全由 LLM 判断——这里只忠实落它的决定。
    for change in deliberation.presence_changes:
        await set_presence(
            lane=lane, persona_id=change.persona_id, room_id=change.room_id
        )

    # 按 event 锚定房间的当前在场集合投递（产生侧在场过滤：信息差命门）
    for draft in deliberation.events:
        recipients = await personas_in_room(lane=lane, room_id=draft.room_id)
        event_id = uuid.uuid4().hex
        for persona_id in recipients:
            await deliver_event(
                lane=lane,
                persona_id=persona_id,
                event_id=event_id,
                summary=draft.summary,
                occurred_at=draft.occurred_at,
                kind=EVENT_KIND_AMBIENT,
                source="world",
                room_id=draft.room_id,
            )

    # 落最新世界快照：world_time = 现实当前时间（每次唤醒都前进）；situation 由
    # LLM 推进（没填就保留上一版）。冷启动也落这一版，让世界有了起点快照。
    await write_world_state(
        lane=lane,
        world_time=now_iso,
        situation=deliberation.next_situation or snapshot.situation,
    )

    # 自排下次醒：**只在 LLM 明确想提前看一眼时**才 emit 一条 self WorldTick。
    # 没给提前需求（next_check_seconds <= 0）就不 emit，靠 wiring 里固定 600s 的
    # interval 保底心跳唯一兜底——避免每轮无条件自排在源循环下线性累积（必改 1）。
    # 提前与否由 world LLM 判断（赤尾宪法）；代码只把它的提前秒数夹到 ≤ 保底心跳
    # （world 不许排长闹钟把世界睡死）后忠实落成一条 emit_delayed。
    if deliberation.next_check_seconds > 0:
        delay_ms = min(deliberation.next_check_seconds * 1000, WORLD_MAX_SELF_WAKE_MS)
        await emit_delayed(
            WorldTick(lane=lane, reason="self"),
            delay_ms=delay_ms,
        )


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

    life emit 的意图字段（persona_id / summary）翻进 WorldTick 的 intent_*，
    让 world 被 reason="intent" 唤醒、把意图内容透给 LLM 裁决。这是 life → world
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
            intent_persona_id=intent.persona_id,
            intent_summary=intent.summary,
        )
    )
