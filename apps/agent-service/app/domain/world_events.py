"""World/life event 流转骨架的 Data 形态 — Task 1.

赤尾世界靠 event 推动。event 有两类来源：

  * ``ambient``   环境感知（含在场说话 / 喊话 —— 说话是动作、声音是 ambient），
                  由 world 客观投影产出。

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

形态扩展：event 形态不写死成只能装环境感知—— ``kind`` 是开放字符串。后续
"直连 / 地点痕迹"需要的结构化负载（一个 JSONB
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
#   * ``message``      隔手机发来的消息（life proactive messaging / send_message 工具）：
#                      某角色不在一起时给收件人**手机发消息**（``source`` = 发送者
#                      persona_id），直投收件人信箱、**不经 world**。和 ``speech``（当面
#                      说话）是收件人侧必须可区分的两个模态（spec 决策 5：否则又把「当面
#                      还是手机」混为一谈）——life context 应把它呈现成「X 给你发消息：内容」
#                      区别于 speech 的「X 对你说：原话」（呈现是 task 5 的职责）。和 speech
#                      同样唤醒收件人（手机消息发给对方就是让 ta 看到的，非被动 kind）。
#                      只用于角色↔角色的手机消息；给真人发飞书私聊不落信箱、走出站段。
#   * ``idle_sense``   闲时刻主动周遭切片（life-idle-wake-via-sense Task 1）：world 用
#                      sense 逐角色推演周遭时，若判断 recipient 此刻处于天然的闲时刻
#                      （刚起床 / 刚做完一件事 / 饭后窝着这类——对她上次已知状态做时间外推，
#                      不是等她先产生一个新动作），就把 ``idle=True`` 传给 sense。判断
#                      准则里明确划了两条边界（见 world_loop_instruction 的 sense 段）：
#                      正在安睡不算天然的闲、同一个没怎么变的静止场景不逐轮机械重复
#                      判——这堵的是历史事故的复现（world 每轮都判"仍是天然闲时刻"、
#                      每 30 分钟真敲一次门，把自排睡着的姐妹吵醒睡不满，当初就是因为
#                      这个才把 sense 整体改成被动通道）。内容形态与被动 ``surroundings``
#                      完全一样（同一份客观周遭叙事），唯一区别是这次投递**真正唤醒她**
#                      ——kind 不同让即时敲门（``deliver_event``）与补敲对账
#                      （``list_personas_with_unread``）两条路径天然读到同一个"这是
#                      主动"的信号，不需要额外的 wake 参数（历史教训：独立 wake 参数
#                      只挡即时敲门、挡不住补敲，见 :data:`PASSIVE_EVENT_KINDS` 的注释）。
#                      渲染时不能套用 ``surroundings`` 的"上一次感知到的周遭...可能
#                      已经变了"旧快照框（这条是唤醒她的理由，不是被动积压的旧背景），
#                      但也不能无条件断言"此刻正在发生"——信箱积压 / cd 重排 / 补敲对账
#                      都可能让送达延迟，渲染侧要带诚实的感知时刻时间锚（见 life_wake
#                      的 ``_format_idle_sense``），态度介于两者之间。
#   * ``own_chat_reply`` chat 对真人的回复已经发出去了（chat/life 并发重复回复修复）：
#                      chat_node 确认一段回复内容确实要发给真人后，把这段回复原文
#                      ``deliver_event`` 进对应 persona **自己**的信箱，让并发被别的
#                      event 唤醒的 life 也能在 ``list_unread_events`` 里可靠读到「我刚
#                      对这次交互回复了什么」，不再依赖对 ``common_message`` 异步落库
#                      时序的假设（旧路径：chat 只 emit 不带内容的 ``EventArrived``，
#                      life 醒来时实时查 ``common_message`` 猜"有没有回过"，channel-server
#                      落库慢于 5s debounce 触发时会误判、重复生成一次相近回复）。不是
#                      "不主动唤醒、下次自然醒来才读到"的被动 kind——**仍然主动唤醒**
#                      life（不在 ``PASSIVE_EVENT_KINDS`` 里），只是内容来源从"猜"变成
#                      "确定收到"，所以不复用 #279 删除的被动 ``EVENT_KIND_EXTERNAL_
#                      PASSIVE``，另起一个语义清晰的新 kind。``source`` 固定为
#                      ``"chat"``（区别于姐妹直投 speech/message 的 persona_id 来源）。
EVENT_KIND_AMBIENT = "ambient"
EVENT_KIND_SURROUNDINGS = "surroundings"
EVENT_KIND_SPEECH = "speech"
EVENT_KIND_MESSAGE = "message"
EVENT_KIND_IDLE_SENSE = "idle_sense"
EVENT_KIND_OWN_CHAT_REPLY = "own_chat_reply"


# 被动 event kind——单一定义处，写 / 读两端都从这里取（宪法「禁止重复定义」）。语义：
# 这些 kind 是**被动上下文**、本身不唤醒她。它的到来本身不敲门、不补敲、不打断长睡；它
# 会在她**下次被一条客观动静（走 EventArrived 敲门）唤醒**时，一并被读进 stimulus 里的
# 未读项（list_unread_events 把信箱里所有未读项都带出来，被动项跟着这次主动唤醒一起被
# 读到）。当前被动 kind：
#
#   * ``surroundings``（**通道分离的权宜修复 v2**，prod 节奏失控）：world 五官每轮给三姐妹
#     各投一条周遭切片。world ~30 分钟推一轮、每轮投一条，若走唤醒通道会把自排睡着的姐妹
#     全敲醒、自排睡眠系统性睡不满——prod 节奏失控的根因。
#
# 把被动语义落在**已持久化的 kind** 上（而非投递瞬间的 wake 参数），让**即时敲门**
# （deliver_event）和**补敲对账**（list_personas_with_unread → renotify_unread）两条
# 路径都读同一处跳过被动——上一版只给即时敲门加 wake=False，没挡住 world engine 每轮调
# 的补敲（被动项入信箱就是"未读"、补敲照样叫醒），修复被绕过。
#
# **surroundings 那条是已知权宜修复、非完美方案。** 粗在"唤醒 vs 不唤醒"二分——被动 kind
# 完全不唤醒会让她对"该早点注意、但还没到 notify 级"的周遭变化有感知延迟（最坏延到她下次
# 被一条 ambient 动静唤醒时才读到）。更优方案（按变化显著度分级、或让 world 显式判这条切片
# 要不要打断长睡）待探索，详见 memory ``project_world_sense_wake_tradeoff``。
#
# ``idle_sense`` **有意不在这个集合里**（life-idle-wake-via-sense Task 1）：它是
# sense 的主动变体——world 判断 recipient 此刻天然闲着时投的周遭切片，就该像
# ambient / speech / message 一样走唤醒通道。把它排除在 ``PASSIVE_EVENT_KINDS`` 之外，
# 就让即时敲门和补敲对账两条路径**不需要任何新代码**就天然一致地把它当"真动静"处理
# ——这正是"wake 判定统一走 kind 归属"设计决策的落点。
PASSIVE_EVENT_KINDS = frozenset({EVENT_KIND_SURROUNDINGS})


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
    kind: str            # ambient | surroundings | speech | message | idle_sense | future extension
    source: str          # 产出方：world / 说话者 persona_id / chat ...
    summary: str         # 客观可感形态的文字描述（或 surroundings 的周遭客观切片）
    occurred_at: str     # event 发生时间 (ISO8601)
    # 会话身份：历史 chat 回灌设计留下的 nullable 字段。当前 chat→life 对话感知改为醒来
    # 时实时读取 common_message，新 chat 路径不再写这些字段；字段保留是 durable schema
    # forward-only 约束，避免旧列被迁移层当成删除。
    #
    # 历史语义：
    #   * ``chat_id``    = ``common_conversation_id``（群 uid = ``"group:" + chat_id``）。
    #   * ``chat_scope`` = DB 原值 ``direct`` / ``group``（不映射成 p2p/group，读侧据它判群）。
    #   * ``chat_name``  = 群名（``common_conversation.display_name``），私聊 / 查不到为 None。
    # 三字段都 **nullable、默认 None**。durable schema 变更是 **forward-only**（加 nullable
    # 列对上线安全：migrator additive ``ADD COLUMN`` 不带 NOT NULL、不触发 fail-closed；旧
    # 条目这三列为 NULL 读写不炸）。
    chat_id: str | None = None
    chat_scope: str | None = None
    chat_name: str | None = None


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
