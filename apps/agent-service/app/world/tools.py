"""world 的 agent 工具集 — 续写四工具 + 反思独占的翻页工具（Task 2b 拆分）.

world 不再"填一张表"返回结构化对象，而是在一个 agent 循环里**连续调工具去推演
世界**。工具按姿态分成两个物理隔离的工具集（互不干扰不靠嘱咐，靠工具集隔离）：

**续写（``WORLD_TOOLS``，四件）**——顺着流往前推，不质疑前提：

  * :func:`update_world` —— 写一段自然语言、记"世界此刻什么样"。落 durable 快照。
    ``world_time`` 由工具体自填（现实当前 CST，客观时间不让模型编），``detail``
    是模型给的世界叙述，一起 ``write_world_state`` append 一版。
  * :func:`notify`       —— world 推演出"这条客观动静此刻谁够得着"，把 observation
    （自然语言客观描述）投给 recipients（persona_id 列表）。对每个 recipient 包
    ``deliver_event`` 投进其信箱（kind=ambient、source="world"）。谁够得着由
    world 在 prompt 层推演，工具只忠实把它推演出的 recipients 落投递。
  * :func:`sense`        —— world 五官（1C Task 2）：给**单个**角色投她此刻的周遭
    客观切片（你在哪、谁在你身边、环境怎样），``deliver_event`` 进她信箱
    （kind=surroundings）。per-person 形态（区别于 notify 的"一条动静多人够得着"广播），
    逼 world 逐角色推演每人那份切片 —— 这既是五官、也是信息差的守门：每人只拿到为她
    推演的那份，全局视角不经五官反向泄露。**周遭切片走被动通道：kind=surroundings 在
    ``PASSIVE_EVENT_KINDS`` 里，deliver_event 据此只落信箱当被动上下文、不敲门唤醒她**
    （通道分离的权宜修复 v2，prod 节奏失控——world 每轮 sense 会把自排睡着的姐妹敲醒、
    睡不满；被动语义落在持久化 kind 上，即时敲门 + renotify 补敲对账都跳过它，详见 sense
    工具体注释 + memory project_world_sense_wake_tradeoff）。
  * :func:`sleep`        —— 决定下次多久再看一眼世界。它不直接 ``emit_delayed``，
    而是把待办 self-wake 记进本轮 round-scoped state（一轮内多次 sleep 覆盖而非
    追加，最后一次为准），由 engine 在循环收口后 emit 至多一条 self ``WorldTick``
    （避免多次 sleep 叠加未来唤醒风暴）。这是 world 唯一的自排手段。

**反思（``WORLD_REFLECT_TOOLS``，两件）**——对表翻页，质疑前提：

  * :func:`update_arc`   —— 重写「世界走到哪个阶段」（与 update_world 同族、分
    两层钟：detail 写「此刻」明天就过时，世界阶段写「跨周月仍然成立的世界进展」、
    只在翻页级转变时整篇重写）。``turned_at`` 由工具体自填（现实当前 CST，对称
    update_world 的 world_time），``narrative`` 是模型整篇重写的世界阶段全文，一起
    ``write_world_arc`` append 一版（durable、无 dedup，重跑可能 append 语义相同
    的一版——无害，读侧只认最新版）。**只给反思环节**（``app.world.reflection``）：
    续写姿态发现不了「页翻了」（coe 实证：世界阶段 v1 把过期底色结晶了进去），翻页
    归无会话、每日一次、对表现判的反思独占——续写无手碰世界阶段、反思无手碰
    detail / notify / sense / sleep。**不包 @tool_error**：durable 写失败必须炸掉
    整次反思（不落当日标记、同日重试），不能被包成 tool result 假成功。
  * :func:`update_attention` —— 重写「世界当前想看哪」（留给眼睛的关注，与世界阶段
    语义不混放：世界阶段写走到哪、关注写想看哪）。契约与 update_arc 完全同款：
    ``written_at`` 由工具体自填、整篇重写当前仍想看的、**不包 @tool_error**。
    关注的写入方只有反思（单一写入方，闭环每环职责唯一）：续写碰它会把拮抗姿态
    混回去、眼睛只如实报告看到什么不决定看什么——续写与眼睛都无手碰关注。

工具体从 ambient :class:`~app.agent.context.AgentContext` 读 world 本轮的
``lane`` 和 ``round_id``（塞在 ``features`` 里）——lane / round 是机制层的事，
不是世界内容决策，所以不放进工具签名让模型填。``round_id`` 是本轮唤醒的确定性
标识，:func:`notify` 用它把 event_id 派生成确定的（lane + observation + round_id），
整轮重放时同一条动静落同一 id、靠 ``deliver_event`` 幂等去重不重复。

赤尾设计宪法：
  * :func:`notify` 的 observation 必须是客观可感形态、绝不写情绪 / 主观解读（这条
    在 prompt 层钉、工具只忠实落模型给的文字）。
  * 谁够得着由 world 推演（在 prompt 层判断），工具不查表、不机械匹配房间。
  * :func:`sleep` 的 60s~1h 上下限是"别睡死 / 别排太密"的机制边界（决定何时醒），
    不是用阈值替"产什么"的决策。超限**返回错误喂回模型让它重调**，绝不静默夹。
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Annotated

from pydantic import Field

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import get_or_create_counter, tool_error
from app.data.queries.mailbox import deliver_event
from app.domain.world_events import (
    EVENT_KIND_AMBIENT,
    EVENT_KIND_SPEECH,
    EVENT_KIND_SURROUNDINGS,
    npc_source,
)
from app.infra import cst_time
from app.runtime.emit import emit_delayed  # module-level so tests can monkeypatch
from app.world.arc import write_world_arc
from app.world.attention import write_world_attention
from app.world.state import (
    read_world_state,
    set_next_wake_at,
    write_world_state,
)

logger = logging.getLogger(__name__)

# sleep 上限：world 最长睡 1h。超限报错喂回模型，绝不静默夹。这是"别睡死"的机制
# 边界，不进世界内容决策（赤尾宪法）。
WORLD_SLEEP_MAX_SECONDS = 3600

# sleep 下限：world 自排最短 1 分钟。低于下限报错喂回模型重调（跟上限超限处理对称、
# 不静默夹），让 self 自排不排得太密。pull 范式下 act 不再唤醒 world（act 只落库、
# world 醒来按游标 pull），所以唤醒频率完全由这条 sleep 决定，没有别的旁路把 world
# 拽起来。
WORLD_SLEEP_MIN_SECONDS = 60

# round-scoped 可变 state 的 features key（engine 每轮新建、工具体跨调用读写、
# engine 收口后读取）。待办 self-wake 让一轮内多次 sleep 覆盖而非累积（唤醒风暴
# 命门，最后一次为准）。
FEATURE_SELF_WAKE = "world_self_wake"  # {} | {"delay_ms": int} —— 本轮待办 self-wake

# event_id 派生命名空间：固定 UUID，让 uuid5 在同一 (lane, observation, round) 上
# 稳定可复现（整轮重放幂等命门）。
_EVENT_ID_NS = uuid.UUID("6f1c8b2a-5e7d-4c3a-9f0b-1d2e3a4b5c6d")

# 周遭切片（sense）的 event_id 派生命名空间：与 notify 的 _EVENT_ID_NS 分开，让同样
# 文字的「周遭切片」与「动静」派生出不同 id、不在 deliver_event 幂等里互相吞掉
# （两类是不同语义的 event）。
_SURROUNDINGS_EVENT_ID_NS = uuid.UUID("9a3d7c1e-2b4f-4e6a-8c0d-7f1a2b3c4d5e")

# NPC 来访（npc_visit）的 event_id 派生命名空间：与 notify / sense 都分开，让同样文字
# 的「NPC 对你说的话」「动静」「周遭切片」派生出不同 id、不在 deliver_event 幂等里互相
# 吞掉（三类是不同语义的 event）。
_NPC_EVENT_ID_NS = uuid.UUID("3c5b8e1d-4a2f-4d6c-9e7b-0a1f2c3d4e5f")

WORLD_NOTIFY_EVENTS = get_or_create_counter(
    "world_notify_events_total", "world notify 投递的 event 计数", ["status"]
)
WORLD_SENSE_EVENTS = get_or_create_counter(
    "world_sense_events_total", "world sense 投递的周遭切片计数", ["status"]
)
WORLD_NPC_VISIT_EVENTS = get_or_create_counter(
    "world_npc_visit_events_total", "world npc_visit 投递的 NPC 来访 event 计数", ["status"]
)
WORLD_SLEEP_REJECTED = get_or_create_counter(
    "world_sleep_rejected_total", "world sleep 超出 60s~1h 上下限被拒次数", []
)


def derive_event_id(*, lane: str, observation: str, round_id: str) -> str:
    """确定性派生一条动静的 id：同 (lane, observation, round_id) 同 id。

    整轮重放时模型重复调 notify 同一条 observation，派生出同一 event_id，
    ``deliver_event`` 按 (lane, persona, event_id) 幂等去重，不会重复投递。
    同一 observation 投多个 recipient 共享这一 id（persona 不同自然键不同，
    不冲突）。不同 observation（不同的动静）派生不同 id。谁够得着由 world 推演，
    不靠房间锚点。
    """
    return uuid.uuid5(_EVENT_ID_NS, f"{lane}\x1f{observation}\x1f{round_id}").hex


def derive_surroundings_event_id(
    *, lane: str, recipient: str, surroundings: str, round_id: str
) -> str:
    """确定性派生一份周遭切片的 id：同 (lane, recipient, surroundings, round_id) 同 id。

    与 :func:`derive_event_id` 的两点区别（都是命门）：

      * **把 recipient 纳入派生源**：周遭切片是 per-person 的（绫奈和赤尾这一轮的
        切片即便文字偶然一样，也是两条独立 event），收件人不同就该是不同 id，否则
        在 ``deliver_event`` 幂等（自然键含 persona）外、若哪天派生没带 persona 会
        让两人切片误共享。带上 recipient 让派生源天然 per-person、稳健。
      * **独立命名空间**（``_SURROUNDINGS_EVENT_ID_NS``）：同样文字的「周遭切片」
        与「动静」派生出不同 id，不在投递幂等里互相吞掉。

    整轮重放时 world 重复调 sense 同一份切片，派生出同一 event_id，``deliver_event``
    按 (lane, persona, event_id) 幂等去重，不重复投递。
    """
    return uuid.uuid5(
        _SURROUNDINGS_EVENT_ID_NS,
        f"{lane}\x1f{recipient}\x1f{surroundings}\x1f{round_id}",
    ).hex


def derive_npc_event_id(
    *,
    lane: str,
    npc_name: str,
    sister: str,
    what_npc_says: str,
    world_fact: str,
    round_id: str,
) -> str:
    """确定性派生一件 NPC 来访的 id：同 (lane, npc, sister, 话, world_fact, round_id) 同 id。

    NPC event 是 durable 写（投信箱），整轮 ``Agent.run`` retry 会重放它。派生源绑
    触发源（轮 id + 哪个 NPC + 指向哪个姐妹 + 说的话 + 这件事的世界事实），让重放投
    同一条 event，``deliver_event`` 按 (lane, persona, event_id) 幂等去重、不重复投
    ——与 notify / sense 的 round_id 幂等做法同一套（不另搞）。

    派生源里四个命门各自防一类误吞：

      * ``npc_name``：同一轮林小满找绫奈、顾舟找绫奈是两件独立 event（不同 NPC）。
      * ``sister``：林小满分别找绫奈和找赤尾也是两件（不同收件人，理论上 world
        可一轮推多件）。
      * ``what_npc_says``：她说的不一样自然是两件事。
      * ``world_fact``（codex 必改 3）：同一姐妹、同一 NPC、同一轮、同一句话但
        **不同 world_fact** 是两件客观上不一样的来访（先发消息约、过一会儿又来
        电话敲定，恰好那句话措辞相同）——不纳入它，第二件会撞同一 id 被幂等当
        重放吞掉。纳入它让不同事不撞、同事（整轮重放：四样全同）仍幂等。

    独立命名空间（``_NPC_EVENT_ID_NS``）让同文字的 NPC 话 / 动静 / 周遭切片不撞。
    """
    return uuid.uuid5(
        _NPC_EVENT_ID_NS,
        f"{lane}\x1f{npc_name}\x1f{sister}\x1f{what_npc_says}"
        f"\x1f{world_fact}\x1f{round_id}",
    ).hex


def _world_round() -> tuple[str, str]:
    """从 ambient context 取 world 本轮的 (lane, round_id)。

    world_tick 在跑 agent 循环前把 lane + round_id 塞进 ``AgentContext.features``，
    工具体在这里读出来行动。没绑 context 直接 ``LookupError`` 失败快，暴露漏了
    ``agent_context(...)`` 的 wiring bug。
    """
    ctx = get_context()
    lane = ctx.features.get("world_lane", "")
    round_id = ctx.features.get("world_round_id", "")
    return lane, round_id


def _self_wake_slot() -> dict:
    """本轮待办 self-wake 容器 ``{} | {"delay_ms": int}``（engine 每轮新建）。

    sleep 往里写 delay_ms（覆盖而非追加 → 一轮内多次 sleep 最后一次为准），
    engine 在循环收口后读它 emit 至多一条 self WorldTick。
    """
    return get_context().features.setdefault(FEATURE_SELF_WAKE, {})


@tool
@tool_error("更新世界叙述失败")
async def update_world(detail: str) -> str:
    """写下世界此刻的客观叙述（记"世界此刻什么样"）。

    看一眼你记得的上一版世界叙述 + 现在几点，推演世界此刻什么样：谁大概在哪、
    在干嘛、什么氛围（位置融在叙述里）；对收到的角色动作，把它的客观结果也体现
    进来（她去厨房 → 厨房有了动静和她的身影）。detail 只写客观发生了什么，绝不
    写谁的情绪 / 心情 / 主观解读——那是角色自己的事。

    世界时间由系统按现实当前时刻自动记下（你不用、也不能编世界时间）。

    Args:
        detail: 世界此刻的客观叙述（一段自然语言）。

    Returns:
        一句确认文本。
    """
    lane, _round_id = _world_round()
    # world_time 跟现实走，由代码填现实当前 CST（客观时间不让模型编）。
    world_time = cst_time.now_cst_iso()
    await write_world_state(lane=lane, world_time=world_time, detail=detail)
    return "已记下世界此刻的样子"


@tool
async def update_arc(narrative: str) -> str:
    """重写「世界走到哪个阶段」——只在翻页级转变时动（与 update_world 分两层钟）。

    update_world 的 detail 写「此刻的世界是什么样」，每轮都可能重写，明天就过时；
    世界阶段写「跨周月仍然成立的世界进展」，写进去的话明天、下周读仍然为真。判据
    一句话：这句话下周还成立吗？成立才配进世界阶段。

    只有发生了以周月计的翻页级转变（考完、放榜、搬家、换季、换工作）才动它，日常
    起居（一顿饭、一节课、一段午后）不动。每次调用都是**整篇重写当前仍成立的世界
    阶段**：翻过去的页被新的一页取代、不是排在后面被追加，绝不写成历史流水账。

    翻页时刻由系统按现实当前时刻自动记下（你不用、也不能编时间）。

    （机制层注意：本工具**故意不包 @tool_error**——write_world_arc 是反思环节
    唯一的 durable 写，写库失败若被包成 tool result 字符串喂回模型，Agent.run 会
    正常返回 → run_arc_reflection 误判成功 → 假成功落当日标记 → 同日重试被吃掉。
    让异常照实穿透（``Tool.invoke`` 的设计语义：未包 @tool_error 即传播）炸掉整次
    反思，由 run_arc_reflection 的 fail-open 接住：不落标记、同日后续轮重试。
    durable mutation 失败要可见，不交给模型在 turn 内自行消化。）

    Args:
        narrative: 当前仍成立的世界阶段全文（整篇重写的自然语言）。

    Returns:
        一句确认文本。
    """
    lane, _round_id = _world_round()
    # turned_at 跟现实走，由代码填现实当前 CST（客观时间不让模型编，对称
    # update_world 的 world_time）。
    turned_at = cst_time.now_cst_iso()
    await write_world_arc(lane=lane, narrative=narrative, turned_at=turned_at)
    return "已翻页，记下了世界阶段的新一版"


@tool
async def update_attention(narrative: str) -> str:
    """重写「世界当前想看哪」——留给眼睛的关注，明早眼睛带着它去看。

    世界阶段写「世界走到哪」，关注写「想看哪」：在等什么消息、想确认什么事、什么时候
    该再看一眼——这些关切写在这里，眼睛明早会带着它去看，把看到的（或没看到的）
    带回当日底料。

    每次调用都是**整篇重写当前仍想看的**：看完的、不再关心的被新版**取代**，
    不是追加成清单。当下没有要看的，也要重写一版说明「没有特别要看的」——这就是
    清空：旧关注只有被新版取代才会消失，不写这一版，眼睛明早还会带着过时的关注
    去看。

    写下时刻由系统按现实当前时刻自动记下（你不用、也不能编时间）。

    （机制层注意：本工具与 update_arc 同理**故意不包 @tool_error**——
    write_world_attention 是反思环节的 durable 写，写库失败若被包成 tool result
    字符串喂回模型，Agent.run 会正常返回 → 反思误判成功 → 假成功落当日标记 →
    同日重试被吃掉。让异常照实穿透炸掉整次反思，由反思的 fail-open 接住：不落
    标记、同日后续轮重试。durable mutation 失败要可见，不交给模型在 turn 内
    自行消化。）

    Args:
        narrative: 当前仍想看的关注全文（整篇重写的自然语言；没有要看的就写一版
            说明没有）。

    Returns:
        一句确认文本。
    """
    lane, _round_id = _world_round()
    # written_at 跟现实走，由代码填现实当前 CST（客观时间不让模型编，对称
    # update_arc 的 turned_at）。
    written_at = cst_time.now_cst_iso()
    await write_world_attention(lane=lane, narrative=narrative, written_at=written_at)
    return "已记下当前的关注，明早眼睛会带着它去看"


@tool
@tool_error("投递动静失败")
async def notify(recipients: list[str], observation: str) -> str:
    """把一条客观动静投给你推演出"此刻够得着它"的角色。

    observation 必须是客观可感的形态（感官投影），比如"厨房飘来煎蛋和咖啡的香味"
    "玄关传来开关门的声音""晌午的光斜照进房间"。绝不写情绪、心情、主观解读、
    建议或指令。

    recipients 是你推演出此刻够得着这条动静的角色 id 列表（在厨房的人闻得到厨房
    的香味、在学校的够不着）——谁够得着由你判断，不是查表。没人够得着就传空列表。

    Args:
        recipients: 此刻够得着这条动静的角色 id 列表（如 ["chinagi", "ayana"]）。
        observation: 客观可感形态的文字描述。

    Returns:
        一句确认文本，含投给了谁。
    """
    lane, round_id = _world_round()

    if not recipients:
        WORLD_NOTIFY_EVENTS.labels(status="nobody").inc()
        return "这条动静此刻没人够得着，没投给任何人"

    event_id = derive_event_id(lane=lane, observation=observation, round_id=round_id)
    # occurred_at 跟现实走，由代码填（客观时间不让模型编）。
    occurred_at = cst_time.now_cst_iso()
    # 逐个独立投递：一个 recipient 失败不影响其他人，失败的 persona log 出来。
    # 整条 notify 不因单人失败被 @tool_error 包成错误炸掉。
    succeeded: list[str] = []
    failed: list[str] = []
    for persona_id in recipients:
        try:
            await deliver_event(
                lane=lane,
                persona_id=persona_id,
                event_id=event_id,
                summary=observation,
                occurred_at=occurred_at,
                kind=EVENT_KIND_AMBIENT,
                source="world",
            )
            succeeded.append(persona_id)
        except Exception:
            failed.append(persona_id)
            logger.warning(
                "world notify 投递给 %s 失败（lane=%s event=%s），其余收件人不受影响",
                persona_id,
                lane,
                event_id,
                exc_info=True,
            )
    WORLD_NOTIFY_EVENTS.labels(status="delivered").inc()
    if not succeeded:
        return f"这条动静投递都失败了（{', '.join(failed)}）"
    msg = f"已把这条动静投给 {', '.join(succeeded)}"
    if failed:
        msg += f"（{', '.join(failed)} 投递失败）"
    return msg


@tool
@tool_error("投递周遭切片失败")
async def sense(recipient: str, surroundings: str) -> str:
    """给**一个**角色投她此刻的周遭客观切片（你是她的五官）。

    你有全局视角，但每个角色只能感知到她**够得着**的那一份。所以这条工具是逐角色
    的：你为**这一个** recipient 推演「此刻她在哪、谁在她身边、周围环境怎样」，把这
    份切片投给她——她醒来就能感知到自己所处的周遭，不用问就知道身边有谁、能据此自主
    行动（去找谁、做什么）。

    surroundings 是一段**客观可感**的周遭叙事，从**她**的位置出发写：她在哪个空间、
    此刻谁在她身边或近旁、环境里有什么声响光线气味动静。比如给客厅写作业的绫奈投
    「你在客厅写作业，厨房飘来赤尾做饭的香味，午后的光斜照进来」。绝不写情绪、心情、
    主观解读、建议或指令——那是角色自己的事，你只投客观的周遭。

    **信息差是硬约束**：你的全局视角绝不能整个倒给一个角色。睡着的、出门的、在学校
    的角色，她的周遭切片就是她那个空间的样子（卧室漆黑安静 / 教室里同学的动静），
    **不该**包含她够不着的别处（厨房在发生什么、客厅有谁）。逐角色分别推演、各投各的
    切片，就是信息差的守门。

    Args:
        recipient: 这份周遭切片投给谁（单个角色 id，如 "ayana"）。
        surroundings: 从她的位置出发的、此刻她周遭的客观可感叙事。

    Returns:
        一句确认文本。
    """
    lane, round_id = _world_round()

    event_id = derive_surroundings_event_id(
        lane=lane, recipient=recipient, surroundings=surroundings, round_id=round_id
    )
    # occurred_at 跟现实走，由代码填（客观时间不让模型编）。
    occurred_at = cst_time.now_cst_iso()
    try:
        # kind=EVENT_KIND_SURROUNDINGS 本就在 PASSIVE_EVENT_KINDS 里——周遭切片走
        # **被动通道**：deliver_event 按 kind 判断不敲门唤醒（通道分离的权宜修复 v2，
        # prod 节奏失控）。world ~30 分钟推一轮、每轮 sense 给三姐妹各投一条周遭切片；
        # 若走唤醒通道（设计上永远放行、不走"到点才醒"的 gate）会把自排睡着的姐妹全敲
        # 醒、自排睡眠系统性睡不满。改为被动：她下次自己醒来（self-wake 到点）时
        # list_unread 自然读到最新周遭。被动语义统一落在 kind 上（即时敲门 + renotify
        # 补敲对账都读 PASSIVE_EVENT_KINDS），所以这里不再传 wake 参数（它只挡即时敲门、
        # 没挡补敲、是不完整抽象、已删）。权宜解的二分粗 / 感知延迟取舍见 memory
        # project_world_sense_wake_tradeoff。
        await deliver_event(
            lane=lane,
            persona_id=recipient,
            event_id=event_id,
            summary=surroundings,
            occurred_at=occurred_at,
            kind=EVENT_KIND_SURROUNDINGS,
            source="world",
        )
    except Exception:
        WORLD_SENSE_EVENTS.labels(status="failed").inc()
        logger.warning(
            "world sense 投递给 %s 失败（lane=%s event=%s）",
            recipient,
            lane,
            event_id,
            exc_info=True,
        )
        return f"周遭切片投给 {recipient} 失败了"
    WORLD_SENSE_EVENTS.labels(status="delivered").inc()
    return f"已把此刻的周遭切片投给 {recipient}"


@tool
@tool_error("投递 NPC 来访失败")
async def npc_visit(
    npc_name: str, sister: str, what_npc_says: str, world_fact: str
) -> str:
    """让一个有名有姓的固定 NPC（同学 / 同事 / 闺蜜）来找某个姐妹一次。

    世界里那些有名有姓的人（名册里那几位）各自有自己的生活，平时不在画面里，但会因为
    自己的事来跟三姐妹中的某一个发生联系——同桌约绫奈周末、同事找千凪吐槽。当你推演到
    此刻某个 NPC 真会来找某个姐妹时，用这条工具把这件事落下来。它一次做两件事，保证
    「收件人收到」和「世界记得」不分叉：

      * ``what_npc_says`` 直接送进**那一个姐妹**的耳朵里——她下次醒来会读到「{npc_name}
        对你说：……」，据此自然反应。这是这个 NPC 直接对她说的话、原话原样。
      * ``world_fact`` 同步写进世界此刻的叙述里（追加在你记得的上一版世界叙述之后），
        让世界记住「这件事发生过」——你下一轮还记得、别的姐妹也可能从客观面感知到
        （比如绫奈在客厅接了个电话，旁边的人听得到这通电话）。

    分两段写的道理：``what_npc_says`` 是只有收件人听得到的那句话（私人的、冲她来的），
    ``world_fact`` 是这件事**客观可感**的那一面（手机响了、她接起电话、她出门赴约）——
    后者绝不写情绪 / 主观解读，只写客观发生了什么（和 update_world 的 detail 同口吻）。

    NPC 该不该来、来做什么，全由你按世界此刻推演决定——没人催你每轮造个 NPC 出来，
    世界大部分时刻她们都在各自的生活里、不来打扰。即兴的路人（名册里没有的）你也可以
    用这条工具让他来一次，路人不建档、只这一下。

    **一致性语义（codex 必改 2，给维护者看，不给模型看）：** 留世界（write_world_state）
    与投信箱（deliver_event）是两次独立 durable 写、**非事务**——framework 持久化原语
    （insert_append / insert_idempotent）各自开自己的 ``get_session``、各自提交，不接受
    注入 session，要把两写拢进一个事务得改 framework 持久化 API 的契约（牵动所有 Data
    写入方），代价远大于收益，故不做。退而求其次按「先写世界、后投信箱」排序：world
    detail 是世界权威，崩在两写之间的残留是收件人**偶发漏收一次来访**，危害小于反过来
    「收件人反应了但世界不记得」（世界状态与别的姐妹的感知会分叉）。这是 best-effort 残
    留：deliver 失败会 **log error（绝不静默吞）**、并把异常交给 @tool_error 路由给模型，
    世界层那段叙述已落、下次不补投（不重要到值得补偿）。

    整轮重放安全：world 的 ``Agent.run`` 用 ``max_retries=1``（engine 收口处关掉整轮
    重放），所以这条 write_world_state 的 append **不会**被整轮 retry 重放成重复追加；
    模型在同一轮里手动重复调本工具，则靠 ``derive_npc_event_id`` 的确定性 id + deliver
    幂等去重（投信箱不重，世界叙述的重复追加靠 world 自己 prompt 守，不上机制强制——
    一轮里怎么写 detail 是 world 自己的事，赤尾哲学不在代码里强制 world 的决策；这条
    靠 world_loop_instruction 守则②的 prompt 引导）。

    Args:
        npc_name: 来访的 NPC 名字（如「林小满」；名册里的固定人物或你即兴的路人）。
        sister: 这件事指向哪个姐妹（persona_id：akao / chinagi / ayana）。
        what_npc_says: 这个 NPC 直接对她说的话（原话，进她耳朵里）。
        world_fact: 这件事客观可感的那一面（追加进世界叙述，让世界记得；只写客观）。

    Returns:
        一句确认文本。
    """
    lane, round_id = _world_round()

    event_id = derive_npc_event_id(
        lane=lane,
        npc_name=npc_name,
        sister=sister,
        what_npc_says=what_npc_says,
        world_fact=world_fact,
        round_id=round_id,
    )
    # occurred_at 跟现实走，由代码填（客观时间不让模型编，同 notify / sense）。
    now = cst_time.now_cst_iso()

    # 世界留痕（codex 必改：收件人信箱 + 世界层不分叉）。投信箱**之前**先把这件事
    # 同步写进世界叙述：读上一版 detail、把 world_fact 追加进去 append 一版。这一步
    # 由工具自己做、不靠模型另调 update_world 自觉——机制层保证「世界记得这件事」。
    # 冷启动（还没有上一版世界叙述）时 world_fact 直接作为新 detail。
    prev = await read_world_state(lane=lane)
    if prev is not None and prev.detail:
        new_detail = f"{prev.detail}\n{world_fact}"
    else:
        new_detail = world_fact
    await write_world_state(lane=lane, world_time=now, detail=new_detail)

    # 投进那一个姐妹的信箱：kind=speech（有具名说话人、原话直投，对齐 chat 直投的
    # speech 语义）、source=`npc:名字`（机器约定，对齐第一刀 npc_name + 关系页 npc:xxx；
    # 区别于真人的 user:xxx / kind=external、也区别于 world 环境动静的 ambient）。
    # 非事务的 best-effort 残留（codex 必改 2）：世界叙述上面已落，这步投信箱失败要
    # **log error（绝不静默）**、记下哪个 NPC 投给谁失败，再把异常交给 @tool_error
    # 路由给模型——世界层那段已落、收件人偶发漏收一次，危害小于反过来（详见 docstring）。
    try:
        await deliver_event(
            lane=lane,
            persona_id=sister,
            event_id=event_id,
            summary=what_npc_says,
            occurred_at=now,
            kind=EVENT_KIND_SPEECH,
            source=npc_source(npc_name),
        )
    except Exception:
        WORLD_NPC_VISIT_EVENTS.labels(status="deliver_failed").inc()
        logger.error(
            "[npc_visit] %s 来访 %s 已写进世界叙述、但投信箱失败（best-effort 残留："
            "世界记得这事、收件人这次漏收）lane=%s event_id=%s",
            npc_name,
            sister,
            lane,
            event_id,
            exc_info=True,
        )
        raise
    WORLD_NPC_VISIT_EVENTS.labels(status="delivered").inc()
    return f"{npc_name} 来找了 {sister}，已送到她耳朵里、也记进了世界"


@tool
@tool_error("安排下次醒来失败")
async def sleep(
    seconds: Annotated[
        int, Field(description="多少秒后再看一眼世界，必须在 60～3600 之间")
    ],
) -> str:
    """决定过多久再看一眼世界（你唯一的自排手段）。

    看完这一轮，用它定下次多久再醒来看世界。``seconds`` 必须在 60～3600 之间
    （最短睡 1 分钟、最长睡 1h）。超出范围会报错，请改填一个 60～3600 的值重调。

    Args:
        seconds: 多少秒后再看一眼世界（60 ≤ seconds ≤ 3600）。

    Returns:
        一句确认文本。
    """
    if seconds > WORLD_SLEEP_MAX_SECONDS:
        WORLD_SLEEP_REJECTED.inc()
        raise ValueError(
            f"sleep 的 seconds={seconds} 超过上限 {WORLD_SLEEP_MAX_SECONDS} 秒"
            f"（最长睡 1h）。请改填一个 ≤ {WORLD_SLEEP_MAX_SECONDS} 的值重调。"
        )
    if seconds < WORLD_SLEEP_MIN_SECONDS:
        WORLD_SLEEP_REJECTED.inc()
        raise ValueError(
            f"sleep 的 seconds={seconds} 低于下限 {WORLD_SLEEP_MIN_SECONDS} 秒"
            f"（最短睡 1 分钟）。请改填一个 ≥ {WORLD_SLEEP_MIN_SECONDS} 的值重调。"
        )
    # 不直接 emit_delayed：那样一轮内多次 sleep / 多轮 sleep 会各排一条未来 self
    # WorldTick → 叠加 heartbeat 唤醒风暴。改为只把待办 self-wake 记进 round-scoped
    # state（覆盖而非追加 → 一轮内最后一次 sleep 为准），由 engine 在循环收口后
    # emit 至多一条 self WorldTick。
    _self_wake_slot()["delay_ms"] = seconds * 1000
    return f"好，{seconds} 秒后再看一眼世界"


async def fire_self_wake(*, lane: str, self_wake: dict) -> bool:
    """循环收口后 emit 至多一条 self ``WorldTick`` + 落下次该醒时刻（唤醒风暴 + 到点 gate 命门）。

    engine 在 agent 循环跑完后调本函数。``self_wake`` 是本轮 round-scoped 的待办
    self-wake 容器（:func:`sleep` 写的 ``{"delay_ms": int}``，覆盖而非追加 → 一轮
    内多次 sleep 最后一次为准）。有待办时一次性收口三件事（阶段 1B 到点 gate）：

      1. 算目标唤醒时刻 = 现实 now + delay（用现实 CST aware 时间，不用 world_time，
         world_time 会因 gate 停滞）。
      2. 把目标时刻写进 ``WorldState.next_wake_at``（:func:`set_next_wake_at`）——
         唤醒入口对 self / 心跳走 gate 时读它判到点。
      3. emit 唯一一条 self ``WorldTick``，**携带这个目标时刻**（``target_wake_at``）：
         到期时与 state 当前 next_wake_at 比对判 stale（被新自排 / 外部覆盖即作废）。

    写 state 与 emit 携带同一个 target_iso（相等是 stale 判定的命门）。没调过 sleep
    （空容器）就不写、不 emit，靠 10min 保底心跳兜底。返回是否 emit。

    self-wake 的实际投递（``emit_delayed``）留在本模块：world 的自排是工具域的
    事，engine 只在循环收口处触发，firing 机制单一收口在这里。
    """
    delay_ms = (self_wake or {}).get("delay_ms")
    if delay_ms is None:
        return False

    # 目标唤醒时刻 = 现实 now + delay（现实 CST aware ISO，gate 比较的口径）。
    target = cst_time.now_cst() + timedelta(milliseconds=delay_ms)
    target_iso = target.isoformat()

    # 落 next_wake_at（gate 到点判定读它）。写 state 与 emit 携带同一 target_iso：
    # 相等是 stale 判定命门——self 到期时只有携带的目标 == state 当前值才作数。
    await set_next_wake_at(lane=lane, next_wake_at=target_iso)

    # 延迟唤醒在 engine 里 import，避免 tools ↔ engine 循环 import。
    from app.world.engine import WorldTick

    await emit_delayed(
        WorldTick(lane=lane, reason="self", target_wake_at=target_iso),
        delay_ms=delay_ms,
    )
    return True


# 续写工具集（五件）：顺着流往前推。npc_visit 让 world 以具名 NPC 身份投一件指向某
# 姐妹的 event（speech、source=npc:名字）、同步把这件事追加进世界叙述（收件人信箱 +
# 世界层不分叉）。update_arc **不在这里**——翻页归反思环节独占（工具集物理隔离：续写
# 无手碰世界阶段）。
WORLD_TOOLS = [notify, update_world, sense, npc_visit, sleep]

# 反思工具集（两件）：对表翻页 + 留关注。反思环节（app.world.reflection）每日一次、
# 无会话，工具只有 update_arc / update_attention——反思无手碰 detail / notify /
# sense / sleep；反过来续写与眼睛也无手碰世界阶段和关注（姿态物理隔离）。
WORLD_REFLECT_TOOLS = [update_arc, update_attention]
