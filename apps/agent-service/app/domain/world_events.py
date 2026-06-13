"""World/life event 流转骨架的 Data 形态 — Task 1.

赤尾世界靠 event 推动。event 有两类来源：

  * ``ambient``   环境感知（含在场说话 / 喊话 —— 说话是动作、声音是 ambient），
                  由 world 客观投影产出。
  * ``external``  外部消息 —— 用户和某 persona 聊完一次，作为"刚聊过"回灌进
                  她自己的信箱。

新范式：world 退成"世界推演者"，不再是导演 / 裁决者。角色用 ``act`` 自主做事
（自然语言），world 只推演这件事的客观结果、不批准。Data 层的体现是
``ActPerformed``（她做了的事）。**pull 范式**：act 落库但不唤醒 world，world 按
自己 sleep 的节奏醒来时按游标批量读这期间攒的 act 一并推演。

四个 Data：

  * :class:`EventEnvelope` —— durable 信箱条目。每个 (lane, persona, event)
    一行；这是 life 读未读的来源。
  * :class:`EventRead` —— durable 已读标记。每个 (lane, persona, event) 一行
    表示"这个 persona 读过这条 event"。未读 = envelope 里没有对应 read 行的。
    把已读拆成 per-event 行，是为了在结构上杜绝"按 persona 全标"——life 想
    一轮期间新进的 event 天然没有 read 行、永远不会被误吞。
  * :class:`EventArrived` —— transient 敲门信号。信箱来新 event 时 emit，
    走 debounce 攒批一次唤醒 life；内容不在信号里，在 durable 信箱里。
  * :class:`ActPerformed` —— durable 动作记录，某 life 自主做了一件影响外部世界
    的事后直接 ``insert_idempotent`` 落库（不 emit、不唤醒），world 醒来按游标
    pull 它去**推演客观结果**（不是申请裁决）。

lane 隔离：所有 durable Data 的自然键都含 ``lane``。runtime 持久化不会自动
加 lane，不显式带上 coe / ppe 泳道会覆盖 prod 的未读事件（写脏线上客观真相），
所以 lane 进 Key 是定义处的硬约束，不是事后补。

形态扩展：event 形态不写死成只能装环境感知—— ``kind`` 已是 ambient /
external 的开放枚举。后续"直连 / 地点痕迹"需要的结构化负载（一个 JSONB
``payload`` 列）靠 migrator 的 additive ``ALTER TABLE ADD COLUMN`` 加进来；
framework 的 ``insert_idempotent`` / ``insert_append`` 已支持把 dict / list
字段编码进 JSONB 列往返（按声明类型分流），下游 3b 真要结构化负载时直接声明
即可。当前这几个 Data 仍是纯标量，是它们各自的形态选择，不是 framework 限制。
"""

from __future__ import annotations

from typing import Annotated

# insert_idempotent imported module-level so tests can monkeypatch it.
from app.runtime.data import Data, Key
from app.runtime.persist import insert_idempotent

# event kind 协议常量。机制层硬定（不是让 LLM 猜的字符串），消费方按这几类
# 路由 / 解读。
#
#   * ``ambient``      离散动静：环境里出现的某个新声响 / 光线 / 气味（"玄关传来
#                      开关门的声音"），world 用 notify 投给够得着的多人（广播形态）。
#   * ``external``     外部消息：用户和某 persona 聊完一次，作为"刚聊过"回灌信箱。
#   * ``surroundings`` 周遭客观切片（1C Task 2 / world 五官）：world 为**单个角色**
#                      逐角色推演的「此刻你在哪、谁在你身边、环境怎样」客观叙事，用
#                      sense 投给那一个角色（per-person 形态）。它是 life stimulus 里
#                      「此刻你周遭」的底框——区别于 ambient 那种零碎离散动静。裁剪靠
#                      world 逐角色推演产出每人那份切片，不靠任何在场表 / 状态机。
#   * ``speech``       对话原话（1C Task 3 / 角色直连对话）：某角色调 chat 把原话**直投**
#                      收件人信箱（``source`` = 说话者 persona_id），**不经 world**。
#                      收件人醒来在 stimulus 里读到「X 对你说：原话」。区别于 surroundings
#                      （周遭底框）和 ambient（world 推演的离散动静）：speech 是另一角色
#                      直接对她说的话、原话原样、双方各自 transcript 天然承载对话连贯。
#                      world 绝不读 speech 原话——它只从 chat 另一轨的低成本元信息（复用
#                      act 流）知道「有人在交谈」、反映氛围（承重红线，见 chat 工具）。
EVENT_KIND_AMBIENT = "ambient"
EVENT_KIND_EXTERNAL = "external"
EVENT_KIND_SURROUNDINGS = "surroundings"
EVENT_KIND_SPEECH = "speech"


# event ``source`` 协议里 NPC 来访的机读前缀（单一定义处，宪法「禁止重复定义」）。
# NPC 来访以 kind=speech、``source`` = ``npc:名字`` 投递（:func:`app.world.tools.
# npc_visit`），关系页 other_user_id 也用同形态——把 NPC 跟真人（``user:xxx``）、
# 姐妹（裸 ``persona_id``）在 source 命名空间里区分开。这是 event source 契约的一
# 部分，放在协议模块里：write 端（world.tools 投递）、render 端（life_wake 剥前缀
# 呈现）、抽取端（review 从信箱抽 NPC 互动）都从这里取，互不依赖各自的层（life 不
# 准 import world——信息差命门）。
NPC_SOURCE_PREFIX = "npc:"


def npc_source(npc_name: str) -> str:
    """把 NPC 名字拼成机读 event source / 关系页 other_user_id（``npc:名字``）。"""
    return f"{NPC_SOURCE_PREFIX}{npc_name}"


def is_npc_source(source: str) -> bool:
    """这个 event source / other_user_id 是不是一个 NPC（``npc:`` 起头）。"""
    return source.startswith(NPC_SOURCE_PREFIX)


def strip_npc_prefix(source: str) -> str:
    """剥掉机读 ``npc:`` 前缀拿干净 NPC 名字（非 NPC source 原样返回）。"""
    return source.removeprefix(NPC_SOURCE_PREFIX)


class EventEnvelope(Data):
    """durable 信箱条目：一条投递给某 persona 的 event。

    自然键 ``(lane, persona_id, event_id)``：同一条 event 投给同一 persona
    重复投递（mq redelivery）靠 dedup_hash 去重，只进一行。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    event_id: Annotated[str, Key]
    kind: str            # ambient | external | surroundings
    source: str          # 产出方：world / 说话者 persona_id / chat ...
    summary: str         # 客观可感形态的文字描述（或 surroundings 的周遭客观切片）
    occurred_at: str     # event 发生时间 (ISO8601)


class EventRead(Data):
    """durable 已读标记：某 persona 读过某条 event。

    自然键 ``(lane, persona_id, event_id)`` 与 envelope 对齐。标已读 = 为本轮
    实际读到的每个 event_id 插一行；重复标记靠 dedup_hash 幂等。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    event_id: Annotated[str, Key]


class EventArrived(Data):
    """transient 敲门信号：某 persona 信箱来了新 event。

    只当唤醒信号用——内容存在 durable 信箱里。走 debounce 攒批，多条积压只
    唤醒 life 一次。``Meta.transient`` 是 debounce 的硬约束（不落 pg）。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]

    class Meta:
        transient = True


def event_knock_key(arrived: EventArrived) -> str:
    """debounce 攒批分区键：按 (lane, persona) 区分。

    每个 (lane, persona) 自己攒批——不同 persona、不同 lane 互不干扰。Task 3
    把 life-wake 节点接到 ``wire(EventArrived).debounce(key_by=event_knock_key)``
    上时复用这个键，保证攒批分区和信箱隔离口径一致。
    """
    return f"{arrived.lane}:{arrived.persona_id}"


class ActPerformed(Data):
    """durable 动作记录：某 life 自主做的一件影响外部世界的事。

    新范式下角色不再"申请意图待裁决"，而是直接做事（自然语言 ``description``，
    如"我去厨房做饭"）。**pull 范式**：这件事直接 ``insert_idempotent`` 落库、不
    唤醒 world；world 按自己 sleep 的节奏醒来时按游标 pull 它，只去**推演它的客观
    结果**、不批准。durable（非 transient）让动作落 PG 跨进程（life 进程写、world
    进程读）可达且不丢。

    自然键 ``(lane, act_id)``：``insert_idempotent`` 按它去重——act 工具失败重放用
    同一 ``(lane, act_id)`` 再写一次无害（ON CONFLICT DO NOTHING）。lane 进 Key 是
    泳道隔离硬约束（同其它 durable Data 的理由）。动作此刻用一句自然语言
    ``description`` 承载就够，是它的形态选择；真要结构化动作细节时 framework 已支持
    additive 加 JSONB 列。
    """

    lane: Annotated[str, Key]
    act_id: Annotated[str, Key]
    persona_id: str      # 谁做的
    description: str     # 她做了什么,自然语言（如"我去厨房做饭"）
    occurred_at: str     # 做这件事的时刻 (ISO8601)


async def perform_act(
    *,
    lane: str,
    act_id: str,
    persona_id: str,
    description: str,
    occurred_at: str,
) -> None:
    """某 life 自主做了一件事 → ``insert_idempotent`` 落 ``ActPerformed``（pull 范式：不唤醒）。

    life 节点想完一轮、决定做某件事时调用本 helper。act 只悄悄落 PG，**不 emit、
    不走 RabbitMQ、不触发任何唤醒**——world 醒来时按游标批量 pull 这期间攒下的 act。
    用 ``insert_idempotent`` 而非 ``insert_append``：act 工具失败重放会用同一
    ``(lane, act_id)`` 再写一次，``insert_append`` 对无 Version 的 Data 重复插会抛
    UniqueViolation，``insert_idempotent`` 是 ON CONFLICT DO NOTHING、重放无害。
    """
    await insert_idempotent(
        ActPerformed(
            lane=lane,
            act_id=act_id,
            persona_id=persona_id,
            description=description,
            occurred_at=occurred_at,
        )
    )
