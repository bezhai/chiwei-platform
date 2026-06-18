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
from app.agent.sediment import build_life_fold_policy
from app.agent.session import load_session
from app.agent.session_fold import fold_session
from app.agent.trace import collect_usage, make_session_id
from app.data.queries.mailbox import (
    deliver_event,  # module-level so tests can monkeypatch
    list_unread_events,
    mark_events_read,
)
from app.domain.arc_awareness import render_arc_awareness
from app.domain.life_state import find_life_state
from app.domain.notebook import (
    ACTIVE_STATUSES,
    NotebookEntry,
    find_notebook_entry,  # module-level so tests can monkeypatch
    list_notebook_entries,
    render_notebook,
)
from app.domain.thinking_cost import record_round_cost
from app.domain.world_events import (
    EVENT_KIND_AMBIENT,
    EVENT_KIND_MESSAGE,
    EVENT_KIND_SPEECH,
    EVENT_KIND_SURROUNDINGS,
    EventArrived,
    EventEnvelope,
    strip_npc_prefix,
)
from app.infra import cst_time
from app.infra.redis import get_redis
from app.life.living_day import living_day
from app.life.pages import read_day_page_before  # module-level so tests can monkeypatch
from app.life.review import run_day_review  # module-level so tests can monkeypatch
from app.memory._persona import load_persona
from app.nodes.life_tools import (
    build_life_tools,
    fire_schedule_reminders,
)
from app.runtime import node
from app.runtime.data import Data, Key
from app.runtime.debounce import DebounceReschedule
from app.runtime.single_flight import SingleFlightConflict, single_flight

logger = logging.getLogger(__name__)


class ScheduleReminderTick(Data):
    """日程到点提醒信号（备忘录 & 日程 第三块）—— **每条日程各挂各的**独立一路唤醒。

    她 note / edit_note 排了一条带 ``remind_at`` 的日程，收口
    :func:`app.nodes.life_tools.fire_schedule_reminders` 给**每条**各 ``emit_delayed`` 一条
    这个信号（携带 ``entry_id`` + 被排时的 ``remind_at``），到期经 in-process 边接回
    :func:`life_schedule_reminder_node`。

    **为什么是每条各挂各的、独立一路**（调度契约命门）：每条日程有内容（答应明天交作业、
    打算今晚复习），各自能改期 / 取消、可能同时到点，所以每条独立一条 tick（独立
    entry_id + remind_at），互不覆盖、同时到点各走各的。这跟 Task 2 删掉的自设闹钟
    （空时间点、单槽 next_wake_at、丢了就睡死）是两回事：日程丢了顶多「她忘了做某事」
    （真实、可接受的生活后果），所以日程保留、自设闹钟删。

    transient —— 只当唤醒信号（日程内容在 durable 的 NotebookEntry 里）。三键
    ``(lane, persona_id, entry_id)``：泳道隔离 + 每人 + 每条日程一路。``remind_at`` 是被排时
    的目标时刻：到期时与 entry 最新一版的 remind_at 比对判 stale（改期 / 撤时间后旧 tick
    携带值对不上即作废）。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    entry_id: Annotated[str, Key]
    # 这条提醒被排时携带的目标提醒时刻（现实 CST aware ISO）。到期时与 entry 最新一版的
    # remind_at 比对判 stale —— 不一致（改期 / 撤时间）说明这条提醒已过期、判废。
    remind_at: str = ""

    class Meta:
        transient = True


# 日程到点提醒投信箱的 event_id 派生命名空间：固定 UUID，让同一条 reminder
# （lane, persona, entry_id, remind_at）重投派生同一 event_id，deliver_event 按
# (lane, persona, event_id) 幂等去重不重复叫醒（edge 5 不破幂等）。与别处的 event
# 派生空间分开，免得同文字撞 id。
_SCHEDULE_REMINDER_EVENT_NS = uuid.UUID("b7e2c4a1-9d3f-4e8b-a6c5-0f1e2d3c4b5a")
# 投进她信箱的日程到点提醒 event 的 source 标识（机读用，区别于 world / 姐妹 /
# 真人 / NPC 的 source 命名空间）：这是她**自己本子**到点的内部提醒，不是别人来的话。
SCHEDULE_REMINDER_SOURCE = "notebook"


def derive_schedule_reminder_event_id(
    *, lane: str, persona_id: str, entry_id: str, remind_at: str
) -> str:
    """确定性派生一条日程到点提醒投信箱的 event_id（重投幂等命门，edge 5）。

    durable delayed trigger 重投 / 整轮重试会重放同一条 ``ScheduleReminderTick``；投信箱
    的 event_id 从 ``(lane, persona, entry_id, remind_at)`` 确定派生，重投得同一 id、
    ``deliver_event`` 按 (lane, persona, event_id) 幂等去重，不重复叫醒。``remind_at`` 纳入
    派生源让「同一条日程改期后再到点」派生不同 id（不被旧那次去重吞掉）。
    """
    return uuid.uuid5(
        _SCHEDULE_REMINDER_EVENT_NS,
        f"{lane}\x1f{persona_id}\x1f{entry_id}\x1f{remind_at}",
    ).hex


def _schedule_reminder_gate_passes(
    tick: ScheduleReminderTick, entry: NotebookEntry | None
) -> bool:
    """日程到点提醒的到点 gate（照 self-wake 的 stale gate 先例，判这条提醒此刻作不作数）。

    读这条 entry 的最新一版（``find_notebook_entry``），判三件事——任一不满足即判废：

      1. **entry 还在**：``entry is None``（理论上不该发生：日程不物理删、只 append
         done/dropped）→ 判废。
      2. **仍是日程、未划掉 / 未做掉**：``status`` 仍在 :data:`ACTIVE_STATUSES`（她没标
         done / dropped）→ 划掉 / 做掉的日程到点不该再提醒（spec edge 2）。
      3. **remind_at 没被改期 / 撤掉**：entry 当前 ``remind_at`` 仍 == tick 携带的
         ``remind_at`` —— 改期（值变了）/ 撤时间（变 None）后旧 tick 携带值对不上 → 判废，
         新时刻由 edit 时新挂的那条 tick 负责（spec edge 2，stale gate 命门）。

    这是「让她的改期 / 取消真生效、旧提醒不误触发」的机制护栏，不替她决定要不要做这件事
    （到点只把这条推到她面前、她自己处理——赤尾宪法）。
    """
    if entry is None:
        return False
    if entry.status not in ACTIVE_STATUSES:
        return False
    # entry 当前 remind_at 必须 == tick 携带的（改期 / 撤时间后对不上 → stale）。比的是
    # 字符串原值（写时是同一份 ISO，相等即未改期；撤时间后 entry.remind_at 为 None、
    # 与非空携带值天然不等）。
    return entry.remind_at == tick.remind_at


def _derive_life_round_id(
    *,
    lane: str,
    persona_id: str,
    read_ids: list[str],
) -> str:
    """本轮确定性标识，按本轮读到的 event_ids 稳定派生（act_id + turn 幂等靠它）。

    act_id 从 round_id 派生；durable 重投 / 整轮重试要落同一 round_id 才能靠
    (lane, act_id) 自然键幂等去重，所以 round_id 不能从 ``now`` 派生（重投取新时刻 →
    新 act_id → 去重失效）。world-driven wake 下角色只有 EventArrived 一条醒来入口，
    用本轮读到的 event_ids（排序）派生 —— 同一批唤醒重投得同一 round_id → 同 act_id
    （重放幂等不退化），下次同 round_id 重投从 transcript 查到 marker 跳过（turn 幂等）。

    Task 3 的 life round marker（turn 幂等）复用这个 round_id。
    """
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

    同一批唤醒重投得同一 round_id（唤醒只剩 EventArrived 一条入口，按 sorted event_ids
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
# 时长定 45s：把一个 persona 两轮之间的最小间隔压到 45s + 一轮自身耗时，三人合起来
# 最坏几轮/分钟、而非几十，挡住"几乎每轮做事就再被唤醒"的自激。这是机制层的节奏闸
# （跟现有 debounce 窗口同类），不进世界内容决策（赤尾宪法）。注：pull 范式下 act 已
# 不再唤醒 world（act 落 PG、world 按自排节奏 pull），这道 life 侧 cd 只管 life 自己
# 的轮次节奏、与 world 唤醒频率解耦。
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

def _humanize_elapsed(occurred_at: str, now: datetime) -> str | None:
    """把「感知时刻 → now 已过去多久」拼成自然语言时间锚（认知留白维度，刀 3 Task 4）。

    认知留白命门：角色对「别人此刻在哪」的认知全来自 world 投的周遭切片，但她对这条
    信息没有「我是什么时候感知到的、可能已经过时」的留白，于是把可能过时的位置当既成
    事实。这个 helper 给她她**自己感知切片**的时间锚——纯主观的「我多久前感知的」，
    绝不比对 world 真相（那是确定性规则、违反赤尾宪法）。

      * 感知时刻 ≈ now（刚感知、含未来钟漂）→ ``"刚刚"``：刚感知的不硬塞「N 分钟前」
        让她对刚感知的也犯嘀咕，留白的诚实是「多久前就是多久前」。
      * 1 小时内 → ``"X 分钟前"``；超 1 小时 → ``"X 小时前"``（不堆秒级噪声）。
      * occurred_at 脏 / 无法解析（``cst_time.parse`` 返回 None）→ ``None``：算不出可信
        时间锚，调用方降级（只铺周遭原文、留白靠「上一次感知到的周遭」的框兜，不靠这个
        算不出的数字）。

    just-now 阈值取 60 秒：周遭切片就是当前这一刻刚感知的（occurred_at ≈ now）时归入
    「刚刚」，把语义噪声压住而不引入「确定性规则」（这不是替她决策、只是时间锚的措辞档）。
    """
    perceived = cst_time.parse(occurred_at)
    if perceived is None:
        return None
    elapsed_seconds = (now - perceived).total_seconds()
    if elapsed_seconds < 60:
        # 刚感知（含 occurred_at 略晚于 now 的钟漂）：不硬塞 N 分钟前。
        return "刚刚"
    minutes = int(elapsed_seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = int(elapsed_seconds // 3600)
    return f"{hours} 小时前"


def _format_surroundings(surroundings: list[EventEnvelope], now: datetime) -> str:
    """把周遭切片（kind=surroundings）拼成「你上一次感知到的周遭」+ 认知留白时间锚。

    周遭切片是 world 五官为她逐角色推演的「此刻你在哪、谁在你身边、环境怎样」
    （1C Task 2）。它是底框、不是清单条目，所以**不带** ``[kind]`` 机读前缀——直接
    给她当前周遭的客观叙事。一轮可能攒了多版切片（world 多次推演），按发生先后铺开、
    末尾那版是最新的周遭；都喂给她、由她读懂此刻所处。occurred_at 过 ``cst_time`` 归一
    到 CST（同 :func:`_format_dynamics`）。

    **认知留白维度（刀 3 Task 4）**：开头带一行时间锚——「这是你上一次感知到的周遭，
    感知于 X 分钟前」。穿帮根因是她把周遭切片里别人的位置当此刻既成事实（明写「仍在
    家」的赤尾被千凪当成「刚出门」）；时间锚让她意识到她对别人位置的认知只是某个过去
    时刻的快照、可能已变，从而像系统本能做对的那样自然留白（「你要是还在家就慢点走」），
    而不是断言。时间锚按**最新那版切片**的感知时刻算（她对周遭的最新认知有多新）。

    这是优化输入、不是加位置校验 if：只接她自己的 surroundings 切片 + now（她够得着
    的纯主观信息），绝不比对 world 真相纠错（赤尾宪法）。occurred_at 脏 / 算不出时间锚
    时降级（不带数字时间锚），留白仍由「上一次感知到的周遭」的框兜住。
    """
    body = "\n".join(
        f"（{cst_time.to_cst_hms(ev.occurred_at)}）{ev.summary}"
        for ev in surroundings
    )
    # 时间锚按最新那版（末尾）切片的感知时刻算——她对周遭的最近一次认知有多新。
    anchor = _humanize_elapsed(surroundings[-1].occurred_at, now) if surroundings else None
    if anchor is None:
        # 算不出可信时间锚（脏 occurred_at）：降级，只框成「上一次感知到的周遭」+ 原文，
        # 不硬编数字时间锚。
        return f"（这是你上一次感知到的周遭，别人此刻的位置可能已经变了）\n{body}"
    return (
        f"（这是你上一次感知到的周遭，感知于{anchor}——别人此刻的位置可能已经变了）\n"
        f"{body}"
    )


def _group_handle_suffix(ev: EventEnvelope) -> str:
    """来自群的离散动静追加「来自群聊『群名』，群句柄 group:<chat_id>」（task 3）。

    她被白名单**群**消息唤醒时，光有 summary 不够——还要知道这条来自哪个群、并拿到群的
    稳定句柄（``group:<chat_id>``）才能接着在同一个群继续主动说，而不是转头私聊。判群靠
    回灌时落进 event 的 ``chat_scope == 'group'`` + ``chat_id`` 非空（结构化补传，不从
    summary 文本猜）。**群名缺失也兜底展示 ``group:<chat_id>``**（spec 决策 3）：群名只是
    给她认人的，句柄才是她回发到同群的抓手，任何时候都得拿得到。

    群 uid 格式按共享约定直接拼 ``"group:" + common_conversation_id``（不 import 投递目标
    解析层的 group_uid 函数，避免跨 task 代码依赖）。非群 event（旧条目 chat_scope=None /
    p2p）返回空串、照常呈现，不冒群句柄。
    """
    if ev.chat_scope != "group" or not ev.chat_id:
        return ""
    handle = f"group:{ev.chat_id}"
    if ev.chat_name:
        return f"（来自群聊「{ev.chat_name}」，群句柄 {handle}）"
    return f"（来自群聊，群句柄 {handle}）"


def _format_dynamics(dynamics: list[EventEnvelope]) -> str:
    """把离散动静（kind=ambient / external）拼成她"刚感知到的几件事"，按发生先后。

    放 event 的客观可感形态（summary）+ 类型 + 发生时间——都是投进她信箱的、她够得着
    的信息，不含任何 world 全局视角。这些是「环境里出现的新声响光线气味」「刚和谁聊
    过」这类离散动静，区别于 :func:`_format_surroundings` 的周遭底框。event 的
    ``occurred_at`` 在信箱里混着历史格式（chat 写 Unix 毫秒、world 写 CST、life 写
    UTC），显示时一律过 ``cst_time`` 归一到 CST，让她看到的所有时刻是同一个 CST 口径。

    **来自群的动静（task 3）**追加群标注 + 句柄（见 :func:`_group_handle_suffix`）：白名单
    群消息回灌进来时带了会话身份（chat_scope='group' + chat_id），这里把「来自群聊『群名』，
    群句柄 group:<chat_id>」缀在那条后面，让她知道来自哪个群、拿到回发到同群的句柄。
    """
    return "\n".join(
        f"- [{ev.kind}] {cst_time.to_cst_hms(ev.occurred_at)} "
        f"{ev.summary}{_group_handle_suffix(ev)}"
        for ev in dynamics
    )


def _speaker_display(source: str) -> str:
    """把 speech event 的 ``source`` 翻成喂给模型的说话人名字（去机读前缀）。

    speech 的 ``source`` 有两类写法：
      * 姐妹直投（chat）：``source`` = 说话者 persona_id（如 ``akao``）——原样呈现。
      * NPC 来访（npc_visit，NPC 层第二刀）：``source`` = ``npc:名字``（机器约定，对齐
        第一刀 npc_name + 关系页 npc:xxx keying）——呈现时**去掉 ``npc:`` 前缀**，只把
        干净的人名「林小满」喂给模型。前缀是机读用的（关系页 keying、与真人 user:xxx /
        姐妹 persona_id 区分），不该漏给模型看；她读到的就是「林小满 对你说」、自然识别
        是这个 NPC 来找她（不被当真人、不被当 world 环境动静——后两类不走 speech 段）。
        前缀常量与剥前缀逻辑在 :mod:`app.domain.world_events` 单一处定义（event source
        协议层；禁止重复定义，且 life 不准 import world——信息差命门）。
    """
    return strip_npc_prefix(source)


def _format_speech(speech: list[EventEnvelope]) -> str:
    """把别人直接对她说的话（kind=speech）拼成「X 对你说：原话」，按发生先后（1C Task 3）。

    speech 有两类直投来源，都呈现成「X 对你说：原话」让她看清是谁对她说了什么——区别于
    周遭底框（surroundings）和离散动静（ambient）：这是直接冲她来的话、有明确说话人。

      * 姐妹直投：另一角色调 chat 把原话直投进她信箱（``source`` = 说话者 persona_id）。
      * NPC 来访：world 调 npc_visit 以具名 NPC 身份投（``source`` = ``npc:名字``，NPC 层
        第二刀）。说话人名字过 :func:`_speaker_display` 去掉 ``npc:`` 机读前缀再呈现。

    ``occurred_at`` 过 ``cst_time`` 归一到 CST（同其它两类）。对话连贯靠双方各自
    transcript 天然承载，这里只如实呈现收到的每句。
    """
    return "\n".join(
        f"（{cst_time.to_cst_hms(ev.occurred_at)}）"
        f"{_speaker_display(ev.source)} 对你说：{ev.summary}"
        for ev in speech
    )


def _format_messages(messages: list[EventEnvelope]) -> str:
    """把别人隔手机发来的消息（kind=message）拼成「X 给你发消息：内容」，按发生先后（task 5）。

    通信介质维度（spec 决策 5 / 7）：这是**隔着手机/飞书**发来的消息——不在一起时发的，
    不是当面说的。必须与当面 speech 的「X 对你说：原话」收件人侧可区分：一个是手机发的、
    一个是当面说的，混成一句正是「把飞书当当面」的根。

    source 是发送者 persona_id（姐妹互发手机消息，task 3 的 send_message），过
    :func:`_speaker_display` 去机读前缀再呈现（与 speech 同口径）。``occurred_at`` 过
    ``cst_time`` 归一到 CST（同其它各类）。
    """
    return "\n".join(
        f"（{cst_time.to_cst_hms(ev.occurred_at)}）"
        f"{_speaker_display(ev.source)} 给你发消息：{ev.summary}"
        for ev in messages
    )


def _split_perception(
    unread: list[EventEnvelope],
) -> tuple[
    list[EventEnvelope],
    list[EventEnvelope],
    list[EventEnvelope],
    list[EventEnvelope],
]:
    """把未读分成（周遭切片, 当面话, 手机消息, 离散动静），各保持原始（按发生先后）顺序。

    四类语义不同、stimulus 里分层呈现（两个正交维度各自标清，spec 决策 7）：

      * 周遭切片（kind=surroundings）—— world 五官投的「此刻你周遭」底框（物理在场维度）。
      * 当面话（kind=speech）—— 另一角色调 chat 直投的原话「X 对你说：原话」（1C Task 3）。
        当面说的、有明确说话人，独立成层。
      * 手机消息（kind=message）—— 另一角色不在一起时 send_message 隔空发来的消息
        「X 给你发消息：内容」（task 3 / 5）。**通信介质维度**：隔着手机/飞书发的，与当面
        speech 收件人侧必须可区分（spec 决策 5：否则又把「当面还是手机」混为一谈）。
      * 离散动静（其余 ambient / external）—— 环境里出现的新声响光线气味、刚聊过等。

    按 kind 四分（不改各自内部顺序——``list_unread_events`` 已按真实时刻升序）。message
    必须从 dynamics 桶排除（必改 ①）——否则会被 :func:`_format_dynamics` 当离散动静渲染
    成「[message] ...」，与当面话混淆。
    """
    surroundings = [e for e in unread if e.kind == EVENT_KIND_SURROUNDINGS]
    speech = [e for e in unread if e.kind == EVENT_KIND_SPEECH]
    messages = [e for e in unread if e.kind == EVENT_KIND_MESSAGE]
    dynamics = [
        e
        for e in unread
        if e.kind
        not in (EVENT_KIND_SURROUNDINGS, EVENT_KIND_SPEECH, EVENT_KIND_MESSAGE)
    ]
    return surroundings, speech, messages, dynamics


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
            )
    except SingleFlightConflict:
        # 同 (lane,persona) 已有一轮在跑：不并发跑、不写快照、不标已读。交回
        # debounce handler 重排这一批 EventArrived，等当前那轮跑完后再醒一次。
        logger.info(
            "[life_wake] %s/%s another round in flight, reschedule", lane, persona_id
        )
        raise DebounceReschedule(arrived) from None


@node
async def life_schedule_reminder_node(tick: ScheduleReminderTick) -> None:
    """她某条日程**到点了**：走到点 gate，放行就把这条递到她面前、复用敲门把她叫醒。

    这是日程到点提醒的独立一路（spec 第三块），与 event 唤醒并列、互不干扰：它不跑
    life 工具循环、不直接改 LifeState —— 只做「到点把这条日程推到她面前」这一件事，
    她随后在常规唤醒里看到它、自己处理（去做 / 改期 / 划掉），系统不替她判完成、不强制
    执行（赤尾宪法）。

    流程：
      1. **到点 gate**（:func:`_schedule_reminder_gate_passes`）：读这条 entry 最新一版，
         判仍 active、remind_at 仍 == tick 携带值（未改期 / 未撤）。判废就早返不投——
         覆盖 spec edge 2（改期 / 划掉后旧提醒不误触发）、entry 不存在（不该发生）兜底。
      2. **复用「到点把她叫醒」的地基**：放行后 ``deliver_event`` 把这条日程投进**她自己**
         的信箱（kind=ambient、source=notebook），event_id 从
         :func:`derive_schedule_reminder_event_id` 确定派生（重投幂等、edge 5）。
         ``deliver_event`` 投成功后会 emit ``EventArrived`` 敲门——她经常规 life_wake 路径
         被唤醒（到点遇 sleep 时，外部刺激敲门能立刻打断长睡：EventArrived 是角色唯一的
         醒来入口、永远放行不走 gate）。这条日程也已在她每轮唤醒输入的本子段里
         （第二块）以「到点了」呈现，她当场看得到是哪条。

    **edge 1（多条几乎同时到点）**：每条日程各一条 tick、各派生不同 event_id，各走一遍
    本节点、各投一条进信箱——她下一轮一次看到全部到点的，互不覆盖、不漏。
    **edge 5（单飞 / 整轮重试不破幂等）**：本节点不抢 life 单飞锁、不跑工具循环（无
    durable mutation），唯一的副作用 ``deliver_event`` 靠确定性 event_id 幂等去重；被它
    敲醒的常规 life 轮自带单飞锁 + round marker，幂等不受影响。
    """
    entry = await find_notebook_entry(
        lane=tick.lane, persona_id=tick.persona_id, entry_id=tick.entry_id
    )
    if not _schedule_reminder_gate_passes(tick, entry):
        logger.info(
            "[schedule_reminder] %s/%s entry=%s gated out "
            "(remind_at=%s, entry_status=%s, entry_remind_at=%s): "
            "rescheduled / dropped / cleared / missing, skip",
            tick.lane,
            tick.persona_id,
            tick.entry_id,
            tick.remind_at,
            entry.status if entry is not None else "-",
            entry.remind_at if entry is not None else "-",
        )
        return

    assert entry is not None  # gate passed → entry 非空
    event_id = derive_schedule_reminder_event_id(
        lane=tick.lane,
        persona_id=tick.persona_id,
        entry_id=tick.entry_id,
        remind_at=tick.remind_at,
    )
    # occurred_at 跟现实走（客观提醒时刻）。投进她自己信箱：deliver_event 投成功会
    # emit EventArrived 敲门把她叫醒（复用到点叫醒地基）。summary 点出是哪条日程，
    # 让她当场知道（本子段也以「到点了」呈现这条，两处对得上）。
    await deliver_event(
        lane=tick.lane,
        persona_id=tick.persona_id,
        event_id=event_id,
        summary=f"（提醒）你之前记的日程到点了：{entry.content}",
        occurred_at=cst_time.now_cst_iso(),
        kind=EVENT_KIND_AMBIENT,
        source=SCHEDULE_REMINDER_SOURCE,
    )
    logger.info(
        "[schedule_reminder] %s/%s entry=%s due, delivered to her inbox "
        "(remind_at=%s)",
        tick.lane,
        tick.persona_id,
        tick.entry_id,
        tick.remind_at,
    )


async def _run_life_round(
    wake: EventArrived,
    *,
    lane: str,
    persona_id: str,
) -> None:
    """一轮 life 的实际编排（已在单飞锁内）：cd 检查 → 读未读 → 冷启探测 → 跑工具循环 → 收口。

    纯事件反应者：角色被叫醒只剩 EventArrived 这一条入口（world notify / 日程到点提醒 /
    真人聊天都投信箱敲门走它）。它是外部刺激、**不走任何到点 gate、永远跑**——能立刻
    打断长睡。她跑完这一轮就等下一个事件、**自己绝不排下次醒**（Task 2 删自设闹钟整条：
    没有 self LifeWakeTick 执行腿、没有 next_wake_at 意愿写入、没有 fan-out 心跳）。
    存活由世界持续的客观事件流兜底，主动计划走日程（note + 到点提醒）。

    **空信箱 early return**：event 唤醒空信箱（去重命中后的残留信号等）没新动静，不烧
    模型、不写、不标已读。

    **cd 降频（spec 决策 5 第三层）**：开头查 cd key——若上一轮刚跑完、还在 cd 内，就
    ``raise DebounceReschedule(wake)`` 让 debounce handler CAS 重排这批 event 推迟到 cd
    后（延迟 + 合并、绝不 drop）。cd 内不烧模型、不写、不标已读。

    一轮成功收口（标完已读 + 挂本轮排的日程到点提醒）后落一个 cd key（TTL=cd 秒）开启
    下一段冷却。
    """
    redis = await get_redis()
    cd_key = _cd_key(lane, persona_id)
    if await redis.get(cd_key):
        # 还在上一轮的 cd 内：把这批 event 推迟到 cd 后，绝不 drop（攒着、不丢）。走
        # debounce wire、raise DebounceReschedule 让 debounce handler CAS 重排——哨兵只对
        # debounce wire 有意义（EventArrived 的唯一来源就是 debounce wire）。
        logger.info(
            "[life_wake] %s/%s event wake still in cd, reschedule (kept, not dropped)",
            lane,
            persona_id,
        )
        raise DebounceReschedule(wake)

    # 现实此刻时间（CST）：喂 prompt 用它。
    now = cst_time.now_cst()

    # 读 LifeState 拿当前快照（冷启状态恢复段用），这里读一次复用。
    snapshot = await find_life_state(lane=lane, persona_id=persona_id)

    unread = await list_unread_events(lane=lane, persona_id=persona_id)
    if not unread:
        # event 唤醒空信箱（去重命中后的残留信号等）：没新动静，不烧模型、不写、不标。
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

    # 本轮确定性标识，按本轮读到的 event_ids（排序）稳定派生（Task 3 的 turn 幂等 round
    # marker 复用它）：同一批唤醒重投得同一 round_id（重放幂等）。world-driven wake 下
    # 角色只有 EventArrived 一条醒来入口，所以只按 event_ids 派生。
    round_id = _derive_life_round_id(
        lane=lane,
        persona_id=persona_id,
        read_ids=read_ids,
    )

    # act_id 派生：按本轮 event_ids 种子 (lane:persona:sorted(event_ids))——durable 边
    # 重投 / 重试同一批唤醒产同一 act_id，world 按 act_id 幂等消化（重放幂等绝不退化）。
    # capture 进工具闭包，不让模型生成。
    act_seed = f"{lane}:{persona_id}:" + ",".join(sorted(read_ids))
    act_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, act_seed))

    # round-scoped 待挂日程提醒容器（备忘录 & 日程 第三块）：note / edit_note 每带一个
    # remind_at 就往里记 entry_id → remind_at（撤时间记 None，覆盖而非追加 → 同 entry
    # 一轮内最后一次为准），engine 收口 fire_schedule_reminders 给每条有 remind_at 的
    # 日程各 emit 一条 ScheduleReminderTick（每条日程各挂各的）。日程是她真实生活里有
    # 内容的安排，保留；自设闹钟（next_wake_at / schedule）是空时间点维持运转，Task 2 删了。
    schedule_reminders: dict = {}

    tools = build_life_tools(
        lane=lane,
        persona_id=persona_id,
        act_id=act_id,
        observed_at=observed_at,
        schedule_reminders=schedule_reminders,
        # proactive 主动发的历史增量水位：用**本轮进入时**（Agent.run 之前、第 609 行）
        # 读到的快照 observed_at，capture 进工具闭包给 send_message 当 since——只取上一次
        # life 轮之后真人新发的话，治她对着早就说过的旧话反复主动开口。**承重命门**：必须
        # 用进入时的 snapshot 值，不能让工具现读 LifeState——本轮她可能先调 update_life_state
        # 把 observed_at 刷成本轮时刻，现读会让水位被本轮污染、增量永远算空。snapshot 为
        # None（冷启、从没活过一轮）→ 水位 None → since=None → 退回原全量最近 limit 行为。
        proactive_history_since=snapshot.observed_at if snapshot is not None else None,
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
    # （_format_surroundings / _format_dynamics 只取 summary/kind/时间，全是投给她的、
    # 她够得着的），绝不含 world 全局快照。
    # 开头印一行本轮标记（round_id）：写回 transcript 后，下次同 round_id 重投能从
    # session 历史里查到这行 → turn 幂等跳过（对称 world 把标记印进 USER stimulus）。
    # 机读用，对模型无害（它只当是一行元信息）。
    parts = [_round_marker(round_id)]

    # 世界阶段透传（事故修复）：世界阶段（「跨周月公共进展」）翻页后她必须知道——
    # 否则她照 persona 出厂设定过日子穿帮。每轮按本轮唤醒的 lane 读最新一版，渲染成
    # 给"活在里面的人"看的第一人称段（框架文案在 arc_awareness 单一处，无剧情事实）；
    # 空链 / 读失败返回 "" → 整段缺席、不塞占位。位置在机读标记之后、每轮都变的
    # 时刻行与当轮感知之前（稳定前缀区：世界阶段天/周级才变）。信息差不破：世界阶段
    # 写作纪律只写在场所有人都知道的公共进展，全 persona 同享，她读到的是"我本来就
    # 知道的事"——绝不是 WorldState 全局快照（那条命门不动）。
    arc_awareness = await render_arc_awareness(lane=lane)
    if arc_awareness:
        parts.append(arc_awareness)

    # 她最近一页昨天（睡前回顾的产出，life 侧注入）：取日期**严格早于**当前
    # 生活日的最新一版日页——"她记得昨天"。不读 marker（day_reviewed_date 已
    # 降级为观测留痕：清晨回笼觉的快班会把它推前到「今天」、按它取页会把当天
    # 凌晨刚写的短页错当「上一页日子」；对账班补旧日还会把它回拨）。没有更早
    # 的页 → 整段缺席不补占位（诚实的真空）。位置在世界阶段之后、每轮都变的
    # 时刻行之前（稳定前缀区：页天级才变，不打散前缀缓存）。
    # 信息差不破：页是她自己睡前写下的第一人称回顾，本来就是她的记忆。
    # 读页失败绝不杀整个 life 轮（照 render_arc_awareness 的姿势）：注入是上下文
    # 增强，失败只 log warning、整段缺席，本轮照常往下跑。
    try:
        day_page = await read_day_page_before(
            lane=lane, persona_id=persona_id, before_date=living_day(now)
        )
    except Exception as e:
        logger.warning(
            "[life_wake] %s/%s failed to read previous day page, section absent: %s",
            lane,
            persona_id,
            e,
        )
        day_page = None
    if day_page is not None:
        parts.append(
            f"【你睡前写下的上一页日子】（{day_page.date} 那天留下来的几笔，"
            f"写于 {cst_time.to_cst_hm(day_page.written_at)}）：\n"
            f"{day_page.narrative}"
        )

    # 她本子里还没了结的事（备忘录 & 日程 第二块）：每轮唤醒读她**还活着**的条目
    # （active_only=True：她自己没标 done / dropped 的），渲成一段塞进她的输入，让她
    # 带着自己记下惦记的事过日子。**只读、绝不改状态、不删任何东西**（spec 必改点）；
    # **绝不**按年龄 / 条数 / 过期去筛——那是代码替她决定忘掉什么、违宪，进输入的就是
    # 她自己没了结的全部。渲染复用 render_notebook（单一定义处，与 read_notebook 工具
    # / chat inner_context 同一份）。位置在稳定前缀区（时刻行之前：本子按她记 / 改的
    # 频率才变，不打散前缀缓存）。读失败 / 空本子绝不杀整个 life 轮（照 day_page 的
    # 姿势 fail-soft）：空本子 / 读失败 → 整段缺席不补占位，本轮照常往下跑。
    try:
        notebook_entries = await list_notebook_entries(
            lane=lane, persona_id=persona_id, active_only=True
        )
    except Exception as e:
        logger.warning(
            "[life_wake] %s/%s failed to read notebook, section absent: %s",
            lane,
            persona_id,
            e,
        )
        notebook_entries = []
    if notebook_entries:
        parts.append(
            "【你本子里还没了结的事】（你自己记下、还没标做了 / 划掉的，"
            "带着它们过你这一刻）：\n"
            f"{render_notebook(notebook_entries, now=observed_at)}"
        )

    parts.append(f"现在是 {cst_time.to_cst_hm(observed_at)}。")

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

    # 五官分层呈现（1C Task 2 + Task 3 + task 5）：她信箱里有四类，分层呈现两个正交
    # 维度（spec 决策 7）——
    #   * 「周遭切片」（surroundings，world 五官逐角色推演的此刻所处环境，作底框）——
    #     **物理在场维度**（她此刻在哪、身边有谁）。
    #   * 「别人当面对你说的话」（speech，另一角色调 chat 直投的原话「X 对你说：原话」，
    #     Task 3）—— 当面、在身边。
    #   * 「别人隔着手机给你发的消息」（message，另一角色不在一起时 send_message 隔空发的
    #     「X 给你发消息：内容」，task 3 / 5）—— **通信介质维度**：隔着手机/飞书，不是当面。
    #     与当面 speech 收件人侧明确可区分（spec 决策 5：否则又把「当面还是手机」混为一谈）。
    #   * 「离散动静」（ambient / external，环境里出现的新声响 / 刚聊过这类事件）。
    # 分层呈现让她既感知到自己周遭什么样（物理在场）、又看清谁当面说了话 / 谁隔手机发了
    # 消息（通信介质两态）、还知道刚发生了什么动静（四类不互相混淆）。四类都只取自她自己
    # 信箱的未读（_format_* 只取 summary/source/kind/时间，全是投给她的、她够得着的），绝不
    # 含 world 全局快照——信息差命门由 world 逐角色推演产出每人切片守住，不在这里。speech /
    # message 都是直投（不经 world），原话/内容只在收件人这条流里出现、world 那条流绝不含
    # （承重红线，由 chat / send_message 工具双轨守住）。
    if unread:
        surroundings, speech, messages, dynamics = _split_perception(unread)
        if surroundings:
            parts.append(
                "【此刻你周遭】（你此刻所处的环境、身边有谁，由你的感官投射给你）：\n"
                f"{_format_surroundings(surroundings, now)}"
            )
        if speech:
            parts.append(
                "【有人当面对你说话】（就在你身边、直接冲你来的话，按发生先后）：\n"
                f"{_format_speech(speech)}"
            )
        if messages:
            # 通信介质维度（spec 决策 5 / 7）：隔着手机/飞书发来的消息，**不是当面**。
            # 单列自己的段、与当面话「有人当面对你说话」收件人侧明确可区分——治
            # 「把隔空消息当当面」的混淆。
            parts.append(
                "【有人隔着手机给你发消息】（你们这会儿不在一起、隔着手机/飞书发来的，"
                "不是当面说的，按发生先后）：\n"
                f"{_format_messages(messages)}"
            )
        if dynamics:
            parts.append(
                "这会儿你还感知到这些客观动静（按发生先后）：\n"
                f"{_format_dynamics(dynamics)}"
            )
        parts.append("读懂此刻你周遭，过你自己的这一刻。")
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
    #
    # **观测刀**：用 collect_usage() 把 run 包住，截下本轮所有 LLM 调用的 token 用量
    # 落 durable PG（不依赖会系统性丢 trace 的 langfuse）。usage 来自 LLM response，
    # 经 adapter 的 span.update → trace 累加器汇进 collector，run 完读 usage 落库。
    with collect_usage() as usage:
        await Agent(_LIFE_WAKE_CFG, tools=tools).run(
            messages=[Message(role=Role.USER, content=stimulus)],
            prompt_vars=prompt_vars,
            context=context,
            session_id=session_id,
            max_retries=1,
        )

    # 本轮 token 落 durable PG（actor = persona_id），best-effort 吞掉失败：成本观测是
    # 旁路，绝不能因为记成本失败把一轮真实思考搞成失败重投。落库失败只 log，下面的标
    # 已读 / 排下次醒照常进行（swallow 语义在 record_round_cost 里）。
    await record_round_cost(
        lane=lane,
        actor=persona_id,
        round_id=round_id,
        usage=usage,
        observed_at=observed_at,
    )

    # 收口：标已读，只标本轮实际读到的那批 event_id（绝不按 persona 全标）。即使
    # 一次 update 都没调也照常标已读——她看了但没改状态，正常。空信箱已在前面 early
    # return，走到这里 read_ids 必非空。
    await mark_events_read(lane=lane, persona_id=persona_id, event_ids=read_ids)

    # transcript 沉淀折叠（沉淀 Task 2，spec 决策 4/5）：本轮写回已在 Agent.run 里
    # durable 落定（两阶段解耦），这里在同一串行窗口（仍在单飞锁内）做其后的独立
    # 折叠步骤——达到阈值就把整卷压成她自己口吻的沉淀段 + marker 保全行。位置在
    # 标已读之后、挂日程到点提醒之前：沉淀是一次离线 LLM 调用（最长 120s，硬超时见
    # app.agent.sediment），全程占着单飞锁——先把这一步做完再挂提醒，收口顺序稳定。
    # fold_session 整段 fail-open（绝不抛），失败本版不折、下轮再试。也在睡前回顾之前：
    # 回顾读到的就是折叠后形态（沉淀段+近期原文，spec 钉为设计行为）。成本入账在沉淀
    # 回调内自带独立 collect_usage 作用域（actor = f"{persona_id}:sediment"），在上面
    # 本轮 collect_usage 之外调用——绝不混进已落账的本体 usage。
    await fold_session(
        session_id,
        build_life_fold_policy(
            lane=lane, persona_id=persona_id, session_id=session_id, round_id=round_id
        ),
    )

    # 收口挂日程到点提醒（备忘录 & 日程 第三块）：本轮 note / edit_note 排 / 改的每条带
    # remind_at 的日程，各 emit 一条 ScheduleReminderTick（每条各挂各的）。空容器（本轮
    # 没排 / 改日程）不 emit。**Task 2 删自设闹钟后这是 life 唯一的收口排程**——日程是她
    # 真实生活里有内容的安排（到点提醒她去做），区别于已删的自设闹钟（空时间点维持运转）。
    # 逐条失败隔离 + 不往上炸（已 durable 落库的本子不被一条漏挂的提醒拖成失败重投）由
    # fire 内部负责。
    await fire_schedule_reminders(
        lane=lane, persona_id=persona_id, schedule_reminders=schedule_reminders
    )

    # cd 降频（spec 决策 5 第三层）：成功收口后开启一段冷却。落一个带 TTL 的 cd key，
    # cd 内再被唤醒就 reschedule 攒着（见本函数开头）。只在成功跑完才落——撞锁 /
    # 中途失败的轮不落，避免用虚假 cd 卡住真正该跑的下一轮。
    await redis.set(_cd_key(lane, persona_id), "1", ex=_LIFE_CD_SECONDS)

    logger.info(
        "[life_wake] %s/%s ran a round, marked %d read, cd %ds",
        lane, persona_id, len(read_ids), _LIFE_CD_SECONDS,
    )

    # 快班睡前回顾（spec 决策 2 快班；主保证仍是凌晨对账 cron）：只在本轮发生
    # 「**进入睡眠**」的转变时触发（边沿，不是电平）——轮开始读到的快照
    # （函数开头的 snapshot）不是 sleep、收口后她**最新**的主观快照（本轮可能 update
    # 过，所以现读）是 sleep。她睡着时夜里被群消息吵醒跑的轮（轮始轮末
    # 都是 sleep）不再各跑一次回顾——电平触发会让成本随夜间打扰线性放大（旧
    # marker 闸一天一次顺带压住了这个，闸拆掉后暴露）。真正的「再入睡」（醒来→
    # 活动→又睡）是合法转变、照常触发（target = living_day(now)：23:30 入睡=
    # 当日、熬夜 01:30=前一日），当晚就出页、次日凌晨聊天已可用。**无 marker
    # 预检查**（2026-06-12 事故修复）：每次入睡都回顾当前生活日，午睡 / 回笼觉
    # 自然产生中间版、后一次整篇盖前一次（页版本叠加、读侧取最新版是设计行为）
    # ——旧 marker 闸不仅挡掉合法的回笼觉重顾，还让快班把单字段 marker 推前、
    # 坑了对账班。放在全部 durable 收口（标已读 / 排下次醒 / cd）之后：回顾慢 /
    # 失败都不影响本轮收口。run_day_review 自身 fail-open + single_flight 防并发
    # 撞车（绝不向上抛、绝不杀 life 轮）。
    latest = await find_life_state(lane=lane, persona_id=persona_id)
    target_date = living_day(now)
    was_asleep = snapshot is not None and snapshot.activity_type == "sleep"
    if latest is not None and latest.activity_type == "sleep" and not was_asleep:
        await run_day_review(
            lane=lane,
            persona_id=persona_id,
            target_date=target_date,
            now=now,
            trace_session_id=session_id,
            trigger="sleep",
        )
