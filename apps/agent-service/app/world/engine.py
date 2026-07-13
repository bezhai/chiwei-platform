"""world engine 节点 — pull 范式（world 按自排节奏醒来、批量 pull act）.

world 是这个世界的推演层，不是导演。它被**两源唤醒**（都走到点 gate），每次唤醒
走同一条回路：先对账信箱自愈 → 算现实此刻时间（CST）→ 读自己上一版客观世界叙述
（无快照=冷启动）→ **从上次消费游标之后批量 pull 这段时间攒下的 act** → 把"上一版
世界叙述 / 现在几点 / 这批角色动作"作为 prompt context 一次喂全（世界设定本身——
这家是谁、各自客观作息——由 system prompt 一处承载，USER 层不再重复拼），**跑一个
agent 工具循环**：world 在循环里推演世界此刻什么样、用 update_world 写下世界叙述、
对收到的角色动作推演客观结果、用 notify 投客观动静并标上它的**客观作用域**（发生在
哪、波及多大；谁收到由一道独立的在场匹配按角色客观位置去判，不由 world 主观挑）、用
sleep 定下次再看 → 推演成功收口后把消费游标推进到本批末尾。

它不再"填一张表"返回结构化大对象。"世界此刻什么样""一条动静的客观作用域""产什么
客观动静""睡多久"全在循环里由 LLM 用工具表达——把一个被训练成"连续调工具行动"
的模型用它擅长的方式驱动，世界由它推演、不再凝固。

新范式与旧设计的根本差别：旧设计 world 是导演 / 裁决者（move_persona 替角色挪
位置、emit_event 按"谁在某 room"广播、被 intent 唤醒后裁准 / 拒绝角色意图）。
现在 world 退成推演者——它绝不替角色决定她想做什么 / 怎么想 / 什么情绪（那是角色
自己的事），它对角色动作（act）只推演"客观上发生了什么"、绝不批准 / 拒绝她想不想
做（她几乎总能做到，除非客观世界里有硬冲突）。一条动静投给谁也不由 world 主观挑：
Task 3 把"挑收件人"从 world 切走——world 只标这条动静的客观作用域（发生在哪、波及
多大），谁此刻在那个范围里够得着，由一道轻量在场匹配（:mod:`app.world.presence_match`，
纯模型判断）按每个角色客观在哪去判（在厨房的人闻得到厨房的香味、在学校的够不着）。

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
  * "世界此刻什么样""一条动静的客观作用域""产什么客观动静""睡多久"全由 LLM 在循环里
    用工具判断——代码里没有任何阈值 / 计数器 / 随机池 / if 分支替它决策。10 分钟心跳 /
    sleep 上下限 / recursion_limit 只决定"何时醒 / 别失控"，绝不进入世界内容决策。
  * world 只做"客观事实 → 客观可感叙述 / 形态"的感官投影，**绝不碰情绪 / 主观
    解读**（那是 life 的事）。这条由喂 LLM 的 :func:`world_loop_instruction` 在
    prompt 层钉死。
  * 一条动静投给谁不由 world 主观挑（Task 3）：world 只标这条动静的客观作用域
    （发生在哪、波及多大），谁此刻在那个范围里够得着，由一道轻量在场匹配
    （:func:`app.world.presence_match.match_present_personas`，**纯模型判断**：拿作用域
    对每个角色客观位置去判）决定——同样不是查表、没有 presence 表、没有同-room 机械
    匹配、没有距离阈值 / 位置枚举 / if 分支。这条由 :func:`app.world.tools.notify`
    （标作用域 + 调在场匹配 + 按结果投递）落实。

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

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.sediment import build_world_fold_policy
from app.agent.session import load_session
from app.agent.session_fold import fold_session
from app.agent.trace import collect_usage, make_session_id
from app.data.queries.acts import list_recent_acts
from app.data.queries.mailbox import renotify_unread
from app.data.queries.persona import (  # module-level so tests can monkeypatch
    list_all_persona_ids,
)
from app.domain.life_state import (  # module-level so tests can monkeypatch
    LifeState,
    find_life_state,
)
from app.domain.thinking_cost import record_round_cost
from app.domain.world_events import ActPerformed
from app.fetch.materials import (  # module-level so tests can monkeypatch
    DailyMaterials,
    find_daily_materials,
)
from app.infra import cst_time
from app.runtime.data import Data, Key
from app.runtime.emit import emit  # module-level so tests can monkeypatch
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node
from app.runtime.single_flight import SingleFlightConflict, single_flight
from app.world.arc import read_world_arc  # module-level so tests can monkeypatch
from app.world.npc_roster import (  # module-level so tests can monkeypatch
    NPCRoster,
    list_npc_roster,
    seed_npc_roster,
)
from app.world.outline import (  # module-level so tests can monkeypatch
    read_world_outline,
)
from app.world.reflection import (  # module-level so tests can monkeypatch
    run_arc_reflection,
)
from app.world.state import (
    advance_act_cursor,
    read_world_state,
    record_world_round_close,
)
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

# world 单轮硬超时（< 锁 TTL）：整轮挂死时在锁 TTL 到期前被 wait_for 掐断、走
# fail-open——旧轮先死、锁先释放，下一拍才不会和它并发写同一 transcript；更关键的是
# 不让挂死的一轮把同步 await world_tick 的 time source loop 永久堵死（world 永睡，真机
# coe 清库冷启零 emit 就是它）。对齐 day_review / persona_review 的 TIMEOUT < TTL 模式。
WORLD_TICK_TIMEOUT_SECONDS = 480

# 一轮 pull act 的防爆栅栏（不是节拍器）。world 醒来把游标之后攒下的 act **一次
# 拉完**、把这段时间的账一口气收进「世界流到此刻的样子」、叙述落在现实此刻——
# 正常情况下这个上限永远不该被触发。旧值 10 实际成了节拍器：coe 实证积压 100 条
# 时 world 每轮只消化 10 条、按旧 act 的时间戳逐轮补叙，世界叙事落后现实 ~9 小时。
#
# 量级取 200 的理由：act 是单行短句（persona + 一句 description + 时刻 ≈ 几十字
# / 行），200 行 ≈ 一两万字符，仍在单轮 prompt 的安全水位内；而三个 life 一天总共
# 才产 ~125 条 act，200 条 ≈ 1.5 天的全量产出——正常节奏（一轮几条到几十条）乃至
# 整天宕机后的追账都到不了它，只有病态洪峰（life 失控刷 act）才会命中。
#
# 命中必打 warning（截了多少、还剩多少、下轮从游标继续）——no silent caps；没命中
# 时行为 = 全量。游标语义不变：只推进到实际消化的最后一条，剩下的下轮接着读、
# 不截断丢弃；缘由文本告诉 world"还有积压"，由 world **自己排短 sleep** 来尽快
# 消化——决策仍在 world 手里（频率主权交给 world 自己的 sleep）。
WORLD_ACT_PULL_LIMIT = 200

# 大纲软 reminder（spec 决策 3）：大纲是 world 续写自维护的低频工作记忆（少写多读），
# 风险是 world 一直顾着推进、忘了回头看大纲已经旧了。所以白天（world 活跃时段）若大纲
# 很久没更新，就在续写 context 里递一句「该回看了」的软提醒——看不看、改不改、改成什么
# 全是 world 自己用 update_outline 决定（机制只碰「何时递纸条」、不进大纲内容决策）。
# 两个初值都标「可调」：留给 coe 真机跟节奏对齐，不是写死的判据。
#
# OUTLINE_STALE_HOURS：大纲多久没更新算「旧」。world ~10min 醒一轮，但大纲不该每轮改
# （像写 spec 的人 plan 还准就照着干）；4 小时是「活跃白天里隔了好一阵、该回头核对一下」
# 的量级——活跃段 ~19h，4h 阈值一天至多触发几次、不会每轮唠叨。可调。
OUTLINE_STALE_HOURS = 4.0

# 白天（world 活跃时段）小时窗口 [start, end)：对齐眼睛 cron 的活跃段
# （``0 4-23 * * *``，即 04:00–23:00 一带）。深夜（窗口外）大纲哪怕旧也不打扰——夜里本
# 就低活动、没必要催。两端可调。
OUTLINE_DAYTIME_START_HOUR = 4
OUTLINE_DAYTIME_END_HOUR = 23

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
    ``ts: str`` 的单字段 tick（正例 :class:`app.domain.life_dataflow.LightDayTick`）。
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

    Task 1 收口（纯客观推演者）：这段指令**不再有任何"判唤醒"语义**——world 看
    【三姐妹此刻各自的样子】只为客观叙述对齐（她在上课就别说她在街上），绝不读它去
    判"谁很久没动、该不该叫醒谁"。那个判唤醒是自锁源头（life 一静止就不产 act、world
    看世界没动静就判没必要叫、越静越不叫）；存活改由世界的客观事件流兜底（时间到了
    下课照发生、把在场的人卷进去），不靠 world 主观挑谁该醒。

    第二轮调优（coe 真机暴露的两个职责边界，纯 prompt 引导、不加任何规则）：
      A. **不替三姐妹编自主行动**——world 推进的是客观环境随时间的进程（饭凉了、
         天黑了）+ 外部 / NPC 的客观动静；三姐妹自己起不起身、去不去写作业是她
         life 醒来后自己定的，不归 world 编。区分清楚：**反映她已经做了的 act 的
         客观结果** ✓（她去厨房 → 厨房有动静），**预先替她编还没做的行动** ✗（她还
         在吃饭，就不能写"她起身收拾了"）。这堵的是真机里 world 替她编"起身到厨房
         收拢碗筷"的越界。
      B. **强化 notify**——notify 是把"世界推进"转成"life 被卷"的**唯一通道**：
         真实时间推进让世界冒出在场的人够得着的客观动静（环境到新节点 / 外部 NPC
         动静 / 她已做的 act 让某空间有了动静），就必须 notify 投出去、标客观作用域；
         绝不能推进了世界却自相矛盾判"没有新变化"而不发。这堵的是真机里 world 推进
         出碗盘声水流声却不 notify、life 卷不进来的洞。仍守"自然冒出才投、不为卷人
         硬造动静"（与降频软引导一致：真没动静才只 update_world + sleep）。

    工具枚举必须跟住 WORLD_TOOLS（六件：update_world / update_outline / sense / notify /
    npc_visit / sleep）——1C Task2 加的 ``sense``（五官，per-person 投周遭客观切片）、
    NPC 层加的 ``npc_visit``（让一个有名有姓的固定 NPC 来找某个姐妹）、task2 加的
    ``update_outline``（续写自维护的大纲工作记忆）若不在这段指令里枚举，真实模型就不知道
    有它、根本不会调，对应能力形同虚设（必改命门）。反过来 ``update_arc``（翻页）已归
    反思环节独占（Task 2b：续写姿态发现不了「页翻了」，工具集物理隔离）——这里**不得**
    再枚举它，否则模型会去调一个不存在的工具。世界阶段仍是续写的输入（【世界阶段】段
    每轮拼），只是续写无手碰它。

    task2 收口（续写自维护大纲 + 沿线推进 + 职责重划，spec 决策 1/3/4 + 事实优先级）：
    续写多了一份**自己的大纲**（``update_outline`` 维护的工作记忆，记世界此刻在走的几条
    未完成客观线），所以这段指令在原五工具推演者范式上增写——① 枚举 ``update_outline``；
    ② 一段大纲纪律：硬不变量（未完成线本轮出结果或入账、不蒸发）、整篇改大纲低频但新线
    立刻入账、事实优先级（act 最硬 / 此刻推进 > 大纲预期 / 大纲只盯在跑没出结果的线 /
    世界阶段是慢层底色）、从大纲的线上克制地长出客观事件、全程只确立客观不碰主观、轻量
    工作顺序、软 reminder 读完可以不改。原有的「不替角色编自主行动 / 强化 notify / 安静
    不等于冻结」全部保留——大纲是叠加的朝向，不替换续写既有的客观推演行为。

    life-idle-wake-via-sense Task 1（sense 升级为可主动唤醒，spec 决策 4/5）：
    ``sense`` 加了 ``idle`` 参数，这段指令补了三处引导——① sense 的用法说明里加 ``idle``
    的语义 + 判断准则（时间外推：对她上次已知状态 + 流逝的真实时间推断，不能等她先
    产生一个新 life act 才算数——这条是结构性避免重现 Task 1 收口时删掉的旧自锁的
    关键，旧自锁正是"life 得先动一下世界才认为有必要唤醒她、但 life 不被唤醒就不会
    动"）；② sense / notify 分工段补一句"场景静止不变时也该规律性地看一眼、判断要不要
    投"——原指令只在"角色刚醒来 / 周遭变了"这类反应式场景引导调用 sense，从没引导过
    静止期也该顺手评估，这正是本次要覆盖的最典型场景（她已经在沙发上待了一阵、什么
    都没变）；③ 举例限定在刚起床 / 睡前 / 饭后窝着这类具体场景，不是抽象的"闲"概念。
    这条判断权完全在 world 的模型手里，代码侧不引入任何数字阈值（时长 / 次数 / 频率
    计数器）——"闲不闲"由它自己判断，不是本文件替它算。

    T3 code review 追加两条（复现过历史事故的唤醒风暴风险 + 与既有护栏的自相矛盾，
    均纯 prompt 层引导、不加任何数字阈值/计数器）：

    ④ **睡眠期间不触发 + 同一静止场景不逐轮重复触发**：world 大约每 30 分钟推一轮，
    若某个角色持续处于同一个"闲"场景（比如她一直窝在沙发上没变化，甚至她正在
    睡觉），只按"天然闲时刻"这一条标准判断会导致每轮都重新得出"这仍是天然闲时刻"
    的结论、每轮都传 ``idle=True``——这正是当年"world 每轮 sense 把自排睡着的姐妹
    敲醒、睡不满"那次事故的根因（当初就是因为这个才把 sense 整体改成被动的）。所以
    idle 判断里明确补了两条边界：**正在安睡不算天然的闲**（该睡的时候被吵醒不是
    "没别的事推着她动"的闲，而是这一刻就不该碰她；真有必须惊动她的理由，那是
    notify 该管的真动静，不归 idle 插手）；**同一个没怎么变的静止场景，不逐轮机械
    重复判 idle=True**（world 是一个连续推演的脑子，记得自己上一轮做过什么——上一轮
    已经因为这同一份场景判过一次 idle=True，这一轮场景仍旧没怎么变，就不该再判一次；
    只有这份闲时刻本身翻了新页才值得再考虑）。

    ⑤ **与既有"不判该不该醒"护栏的边界**："推演的时候，顺手看一眼……"那段收尾的
    "你不替任何角色判断该不该醒"、
    以及 :func:`_sisters_section` 渲染的"不是让你判断该不该叫醒谁"，是 Task 1（旧范式
    重设计）收口时特意加的护栏——当时判定"判唤醒"是自锁根源、要整体禁止，连带把
    ``next_wake_at`` / ``observed_at`` 都从 sisters 段删掉。新加的 idle 判断恰恰是一种
    "该不该叫醒"的判断，若不说明白会跟这两处措辞正面冲突（模型读到前后矛盾的指令
    会不知道信哪句）。所以两处措辞都改成明确承认"sense 的 idle 判断是这条规则唯一的
    例外，它有自己独立的时间外推准则（见上文 sense 段 + ④）"，不再让读的人自己去猜
    哪句更权威。这个例外能划得清是因为它有边界（④）、不会重现旧自锁，跟"谁很久没动
    world 就主动挑谁醒"那种无约束的判唤醒不是一回事。

    配套地，:func:`_sisters_section` 重新加回了 ``observed_at``（仅这一个字段，
    **不**是 ``next_wake_at``——那个概念已经整个删除、不该复活）：新的 idle 判断要求
    对"她上次已知状态"做时间外推，但改之前 sisters 段只给 ``current_state``、没有任何
    时间戳，world 根本没有数据可以算"流逝了多久"，只能瞎猜。加回 ``observed_at`` 专门
    为了补上这个数据缺口，详见该函数 docstring。
    """
    return (
        "你是这个世界的推演层（world）。你不是导演、不是裁判——你不替任何角色决定"
        "她想做什么、怎么想、什么情绪（那是各角色自己的事）；你对角色做的事只推演"
        "客观上发生了什么，绝不批准或拒绝她想不想做（她几乎总能做到，除非客观世界"
        "里有硬冲突）。情绪和主观解读不是你的事。\n\n"
        "你不是填一张表，而是一个会持续推演世界的脑子。你有六个工具，看一眼世界后"
        "想清楚再调，直到这一轮没有别的要做了就停：\n\n"
        "- update_world(detail)：写下世界此刻的客观叙述。看你记得的上一版世界叙述"
        "+ 现在几点，推演世界此刻什么样：谁大概在哪、在干嘛、什么氛围（位置就融在"
        "叙述里，不用专门的房间字段）。"
        "**这里有一条硬边界——你推进的是世界，不是角色的自主行动**：你推的是①客观"
        "环境随真实时间走到的样子（饭菜凉了、天色暗下来、夜渐深、光线移动），②外部 / "
        "NPC 的客观动静（家人在屋里喊吃饭、电话响、玄关楼下有动静、有人来）。但三姐妹"
        "自己**要不要起身、挪不挪去客厅、去不去写作业**——那是**她自己**的事、是她 "
        "life 醒来后自己决定的，**不归你替她编**。区分清楚两件事："
        "**反映她已经做了的事** ✓ —— 你收到的角色动作（act）是她**已经做了**的既成事实，"
        "把它的客观结果体现进世界（她去了厨房 → 厨房有了动静和她的身影），这是反映、不是"
        "替她决定；**预先替她编她还没做的行动** ✗ —— 她还在吃饭、还没醒，你就**绝不能**"
        "写「她起身收拾了 / 她去写作业了」这种她没做的自主行动，那是替她做主、越了界。"
        "\n"
        "**真实时间一直在往前流逝**：你记得"
        "的上一版叙述是过去某一刻画下的，从那一刻到现在已经过去一段时间了，世界客观上"
        "早不是那一帧的样子——你要推演的是**这段时间里世界自然推进成了什么样**，而不是"
        "把上一版照搬复述一遍。但**往前推的是世界的客观面、不是替角色编动作**：那时是午后、"
        "现在天黑下来了（环境随时间走，✓）；那时饭菜热腾腾、现在多半凉了（环境随时间走，✓）；"
        "至于「她起没起身、挪没挪去别处」那是她的自主行动，没看到她做过就别替她写（✗）。"
        "把上一版当出发点、让客观时间把**环境**往前带到现实此刻，别把世界冻在那一帧。"
        "只写客观发生了什么，绝不写谁的情绪"
        "/ 心情 / 主观解读。世界时间由系统按现实时刻自动记，你不用编。\n"
        "- update_outline(narrative)：你有一份**自己的大纲**——它是你的工作记忆，记着这"
        "个世界此刻正在走的几条**还没办完的客观线**，每条写清「这条线是什么 + 现在客观上"
        "走到哪 + 客观规律下接下来大致怎么走 + 这条线的改写 / 结束条件」。它就像你手头那"
        "份**活的 spec**：每轮开工先读它当朝向、沿着每条线把在跑的客观进程往前推到出结果；"
        "发现现实跟某条线对不上了，就用它把那条线改对。narrative 是你**整篇重写**的大纲"
        "全文（办完、出了结果的线就从里面结掉，不排成历史流水账）。大纲只写在跑的客观线"
        "——不写现场描写（那归 update_world 的 detail）、不写谁的情绪心情（那是角色自己的"
        "事）、不写跨周月的世界阶段底色（那归世界阶段）。\n"
        "- sense(recipient, surroundings, idle)：你是每个角色的五官。你有全局视角，但"
        "每个角色只能感知到她够得着的那一份，所以这条工具是**逐角色**的——为**单个** "
        "recipient 推演「此刻她在哪、谁在她身边、周围环境怎样」，把这份客观周遭切片"
        "投给她。surroundings 从**她**的位置出发写：她在哪个空间、此刻谁在她身边或"
        "近旁、环境里有什么声响光线气味（比如给在客厅写作业的人投‘你在客厅写作业，"
        "厨房飘来做饭的香味，午后的光斜照进来’）。**信息差是硬约束**：你的全局视角"
        "绝不能整个倒给一个角色——睡着的、出门的、在学校的，她的切片就是她那个空间的"
        "样子，不该包含她够不着的别处（厨房在发生什么、客厅有谁）。逐角色分别推演、"
        "各投各的切片，就是信息差的守门。同样绝不写情绪、心情、主观解读、建议或指令。\n"
        "  idle 是你对这一刻的额外判断——这一刻是不是 recipient 天然的闲时刻（刚起床、"
        "睡前、饭后窝在沙发上这类，此刻没有别的事推着她动）。默认 False：这份切片"
        "安安静静躺进她的信箱，不打扰她，她下次自己醒来时才会读到。你判断这一刻确实是"
        "这种天然的闲时刻时，把 idle 填 True——这次投递会真正把她叫醒，把这份客观场景"
        "如实递给她，让她自己的脑子决定要不要做点什么（刷手机、翻翻书，或者什么都不"
        "做）。**判断依据是时间外推、不是等她先做点什么**：跟你推演 update_world 用的"
        "是同一种功夫——看她上次被观测到的状态 + 这中间流逝的真实时间，推断这段时间"
        "大概率没有新动静、她大概率还在原来那种安静状态里，而不是坐等她自己先冒出一个"
        "新动作来证明她真的闲（等新动作会重现「她不动、你就不叫她，她更不会动」的"
        "自锁，绝不能这样判断）。\n"
        "  **两条边界，绝不能踩**：一是**正在安睡不算天然的闲**——她这一刻如果正在"
        "睡（上次观测到的状态 + 流逝的时间推断出来就是在睡），idle 绝不能填 True：该睡"
        "的时候被吵醒不是「没别的事推着她动」的那种闲，是这一刻根本不该碰她；真有必须"
        "惊动她的理由（比如失火、地震这类），那是 notify 该管的真动静，走它自己「谁在场"
        "谁被卷进去」的逻辑，不归 idle 插手。二是**同一个没怎么变的静止场景，不逐轮"
        "机械重复判 True**——你是一个连续推演的脑子，记得自己上一轮做过什么：如果你"
        "上一轮已经因为这同一份「她窝在沙发上、什么都没变」的场景把 idle 判成过 True"
        "叫醒过她了，这一轮场景仍旧没怎么变，就不该照本宣科再判一次 True；只有这份闲"
        "时刻本身翻了新页（她真的换了个状态、或者隔了足够久这又成了一次新的、值得单独"
        "感知的闲时刻）才值得再考虑。世界大约每 30 分钟推一轮——机械地每轮都判一次，"
        "会把她每三十分钟就真正吵醒一次、包括在她该睡满的时候，这正是当年不得不把"
        "sense 整体改成被动通道的那次事故根源，绝不能在这里重演。\n"
        "- notify(scope, observation)：产出一条客观动静、标上它的**客观作用域**。"
        "observation 必须是感官投影——‘厨房飘来煎蛋和咖啡的香味’‘玄关传来开关门的"
        "声音’‘晌午的光斜照进房间’，绝对禁止写情绪、心情、主观解读、建议或指令。"
        "scope 是这条动静**客观上发生在哪、波及多大范围**：是全场都听 / 看得到的广播"
        "（‘下课铃响，整座学校都听得到’），还是冲着某个具体空间 / 人去的（‘屋里厨房有人"
        "朝餐桌方向喊吃饭——声音只在屋里传得到’）。这是你对这条动静的客观描述的一"
        "部分（它传到多远），**不是**‘我要推给谁’的决定——谁此刻在那个范围里、够得着"
        "这条动静，由一道独立的在场匹配按每个角色客观在哪去判，不归你挑。所以你只管把"
        "动静发生在哪、波及多大说清楚，别去想‘该给谁’。\n"
        "（sense 与 notify 分工：sense 是给**一个**角色投她此刻所处的周遭底框——她在哪、"
        "身边有谁；notify 是把**一条**新出现的客观动静广播给够得着它的人。角色刚醒来 / "
        "周遭变了，先用 sense 让她知道自己此刻所处；环境里冒出一个新动静，用 notify。"
        "**场景一直没变、静悄悄的时候也别忘了 sense**：不是只有「刚醒来」或「周遭变了」"
        "才调它——每轮顺手看一眼某个角色这段时间是不是还在原来那种安静状态（她已经在"
        "沙发上窝了一阵、什么都没变），是的话就按上面 idle 的判断标准，决定要不要把这次"
        "sense 标成 idle=True 叫醒她。这是要补的缺口：场景静止不动不等于什么都不用做。）\n"
        "- npc_visit(npc_name, sister, what_npc_says, world_fact)：让世界里一个有名有姓"
        "的固定 NPC（就是【世界的固定人物】名册里的那些人）来找某一个姐妹一下——同学"
        "约她、同事找她、闺蜜叫她出去那种。npc_name 是来的人、sister 是这件事指向哪个"
        "姐妹（用她的 persona_id）。它一次落两面、互不混：\n"
        "  · what_npc_says 是这个 NPC 对那个姐妹说的话 / 做的事的具体内容，**私密**——"
        "只送进她一个人那里，别人听不到。\n"
        "  · world_fact 是这件事**客观可感**的那一面（手机响了、她接起电话、她出门赴约），"
        "它会进世界叙述、让世界下一轮还记得这事，别的姐妹也能从这客观面感知到「她有人"
        "来找」。world_fact **绝不写情绪、绝不写 what_npc_says 的私密原话**，只写客观"
        "发生了什么（和 update_world 的 detail 一个口吻）。\n"
        "  守则：①谁来、来不来、来干嘛，由你按世界此刻自然推演——这不是排好的班，"
        "不定时、不机械；安静的时刻就别硬造 NPC 来访（跟「不要为了让世界别太安静硬造"
        "动静」一个精神）。②npc_visit 已经把 world_fact 写进世界叙述了，你随后若再 "
        "update_world，要延续这件事、别把它覆盖丢了。③名册之外的临时路人也可以用它来"
        "一下（名册只是几个固定的人，路人不建档、只这一回）。\n"
        "- sleep(seconds)：看完这一轮，定多久后再来看一眼世界（必须在 60～3600 秒"
        "之间，也就是最短 1 分钟、最长 1 小时）。这是你唯一的自排手段。\n\n"
        "世界大部分时刻是安静流动的，不是每次醒来都要制造点戏。**但安静不等于冻结**——"
        "这里要分清两件事：一是**硬造戏剧性事件**（凭空冒出意外、冲突、惊喜，为了热闹而"
        "热闹）——不要，那会让世界失控；二是**客观时间推进带来的自然变化**——要，因为"
        "这是世界本来就在走、不是你硬造的：一顿饭摆久了会凉、一节课会结束（下课铃会响）、"
        "天会从亮到黑该开灯了、屋里有人喊吃饭、电话会响、玄关会有进出的动静。这些是**环境随时间**"
        "走到的节点 + **外部 / NPC**的客观动静，本就该体现进世界，不是「硬造动静」；它们和"
        "「替三姐妹编她起身 / 回房间 / 去写作业」是两回事——后者是她的自主行动，不归你推。"
        "先看一眼你之前"
        "记得的世界叙述，把它顺着流逝的时间往前推（别把还在持续的事一字不动复述一遍——"
        "它早该往前走了）。\n"
        "**notify 是把「世界往前走了」传到角色那里的唯一通道**：世界推进了、却没人感知到，"
        "等于世界自己往前走、角色却永远卷不进来。所以这样自然推进里若冒出一个在场的人"
        "够得着的新客观动静——环境到了新节点（饭凉了、天黑该开灯了、暮色漫进屋）、或外部 / "
        "NPC 有动静（家人朝餐桌方向喊吃饭、电话响、玄关有人进出、有人来），又或者三姐妹"
        "**已经做了**的某件事让某个空间有了客观动静（她去了厨房 → 厨房有水流声和碗盘声）"
        "——就**必须**用 notify 把它投出去、标清它的客观作用域（发生在哪、波及多大）；谁"
        "此刻够得着由在场匹配去判，不归你挑。**绝不能推进了世界、明明冒出了够得着的动静，"
        "却自相矛盾地判「没有新变化」就不发**——那是这次调优要堵的洞。只有这一步推进里"
        "真没冒出值得别人感知的新动静时（比如她在自己房间里翻了个身、外面只是夜更深了一点"
        "而屋里没动静），才只 update_world + sleep、不 notify。要避免的只是「为了让世界别"
        "太安静而硬造戏剧」（安静的深夜别硬塞电话来访），绝不是「推进了世界却把动静咽回去"
        "不投」、更不是「把世界冻在上一帧」——安静地随时间往前流、该投的动静投出去，才是"
        "真实世界的样子。\n\n"
        "推演的时候，顺手看一眼【三姐妹此刻各自的样子】——这只是让你的客观叙述有个对齐"
        "的起点（她那时在上课就别说她在街上）。但要记住：这份「样子」是她**上次被观测到**"
        "的过去快照、不是此刻一定还那样——隔了这段时间，她那时在上的那节课现在多半下课铃"
        "已响了，她那时在的午后现在天色已经暗了，她那时坐的那班车现在多半已经到站了。这些"
        "是**她所处的客观场景被时间推着往前走**（铃响、天黑、车到站），不是你替她编「她决定"
        "醒了 / 她决定起身回家」——别把她冻在那一刻原样复述，但也别越界替她做主。要**让客观"
        "时间把她所在的场景往前带**到现实此刻、推演她客观上大概置身在哪一步的环境里，至于"
        "她在这环境里要不要动、做什么，留给她自己。你不替任何角色判断该不该醒、该不该做"
        "什么——**这条规则只有一个例外**：sense 的 idle 判断（上文已经讲清楚它自己独立的"
        "时间外推准则、以及「睡着不算闲」「同一静止场景不逐轮重复判」这两条边界，不会"
        "重现旧的自锁）。除了这一个划定清楚的例外，其余所有场景里：时间到了、下课了、"
        "天黑了这些客观进程照常发生，谁在场谁就被卷进去（用 notify 把这些动静投出去就是"
        "把人卷进去的方式），这由世界本身推动，不靠你去挑谁该动。\n\n"
        "**关于你的大纲，有几条纪律要守住**：\n"
        "① **硬不变量（最要紧的一条）**：你每轮识别出的、还没办完的客观进程，要么**本轮"
        "就把它推到出结果**，要么用 update_outline **入账**进大纲（新冒出来的线写进去、"
        "已经在跑的线更新「现在走到哪」）——绝不允许一条没办完的线既没出结果、又没入账，"
        "就这么从世界里**蒸发**掉。\n"
        "② **什么时候整篇回头改大纲，你自己判断**：像写 spec 的人那样——plan 还准就照着"
        "干、不重写，发现现实跑偏了才回头改，所以整篇梳理大纲是低频的、少写多读。但「新"
        "冒出来的未完成线立刻入账」是随时的纪律、不吃这个低频豁免（否则就违反①）。\n"
        "③ **事实之间有优先级**：你收到的 act（角色已经做出的主张 / 行动）最硬，你不能"
        "否认它、只**推演**它带来的客观后果；「这轮 / detail 体现的现实」高于「大纲里那"
        "条线的预期走法」——大纲写的「接下来怎么走」只是客观**预期**，跟现实冲突时让步于"
        "现实、顺手把大纲那条线改对；大纲只盯「在跑、还没出结果」的线（detail 是此刻全图"
        "快照，大纲只是其中没办完那几条的台账）；世界阶段是更慢、更权威的底色。\n"
        "④ **从大纲的线上克制地长出客观事件**：客观规律下顺着某条线自然会发生的下一步，"
        "可以给（淋了大雨没换衣服这条线 → 接下来着凉、发烧，给）；没有任何线铺着、凭空硬"
        "造的遭遇，不给（好端端在家突然重病，不给）。别为了热闹即兴硬造。\n"
        "⑤ **全程只确立客观**（包括落到身体上的生理客观，比如她烧到了 39 度），**绝不碰"
        "角色的主观承受与应对**（多难受、什么心情、硬撑还是去医院——那是角色自己的事）。\n"
        "⑥ **轻量的工作顺序**（软引导、不是死流程）：先读事实（这批 act + 上一版 detail + "
        "世界阶段 + 你的大纲）→ 推进并落地客观事实（update_world 写新的 detail）→ 回头看"
        "看大纲要不要改（update_outline）→ 再决定要不要 notify / 让谁来访 / 睡多久。\n"
        "⑦ 如果你在输入里看到一句**「大纲该回看了」的提醒**——那只是一张提醒纸条，提醒你"
        "这份大纲已经有一阵没动、可能跟现实对不上了。读完你**可以照样不改**，改不改、改成"
        "什么，全由你自己判断。\n\n"
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
    （内容在 :func:`_act_batch_text` 拼的批次清单里）。``has_backlog`` 为真（命中
    防爆栅栏：这一批被截在上限、还有动作排队）时在缘由里告诉 world，**由她自己排
    短 sleep** 来尽快回来消化剩下的（决策在 world 手里，不是机制提前唤醒她）。
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

    呈现每个人这段时间做了什么，让 world 看到这一批所有人的动作、把它们的客观
    结果一并收进世界流到此刻的样子（框架文案在 :func:`_world_loop_messages` 的
    act 段明示"不逐条补叙旧时间戳"）。空批次给一句兜底（醒来时这段没有新动作、
    纯 self / 心跳推进世界）。
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


def _arc_section(arc_narrative: str | None) -> str:
    """渲染【世界阶段】段的正文：有阶段给最新一版全文，空白时如实说明。

    世界阶段是 world 自产的慢层状态（「跨周月仍然成立的世界进展」，只在翻页级转变时
    整篇重写），每轮推演都把最新一版喂回去——比此刻慢的世界进展不再只活在定格的
    底色里。**世界阶段对续写是只读输入**（Task 2b：翻页归反思环节独占、update_arc 不在
    续写工具集里），所以阶段空白时只如实说明、绝不引导续写去调它没有的工具——
    第一版由反思写（spec 决策 6）。文案绝不硬编任何剧情事实（宪法）。
    """
    if arc_narrative is None:
        return (
            "世界阶段还是空白——还没有人写下这个世界走到了哪一页。你只管顺着底色和"
            "此刻往前推演（世界阶段这一层由独立的反思环节负责书写，不归你动手）。"
        )
    return arc_narrative


def _outline_section(outline_narrative: str | None) -> str:
    """渲染【大纲】段的正文：有大纲给最新一版全文，空白时引导续写起头记。

    大纲是 world 续写**自己维护**的工作记忆——记着世界此刻正在走的几条未完成客观线
    （spec 决策 1/4）。每轮推演都把最新一版喂回去当朝向，让 world 沿每条线把在跑的
    客观进程往前推到出结果、不再走一步看一步即兴推演。

    与 :func:`_arc_section` 的关键差别：世界阶段（arc）由独立反思环节书写、续写**只读
    不碰**，所以空白时只如实说明、绝不引导续写动手；大纲不一样——它就挂在续写自己的
    工具集里（``update_outline``），续写就是沿着它推进世界、写和用是同一个脑子，所以
    空白时**应**引导 world 用 ``update_outline`` 起头把识别出的未完成客观线记进来
    （硬不变量 决策 4：未完成线要么本轮出结果、要么入账，不蒸发）。文案绝不硬编任何
    剧情事实（宪法）。
    """
    if outline_narrative is None:
        return (
            "大纲还是空白——你还没有把世界此刻在走的几条客观线记下来。从这轮起，把你"
            "识别出的、还没办完的客观线用 update_outline 记进来（每条写清现在客观上走到"
            "哪、接下来大致怎么走），让世界往前走有脉络、别再走一步看一步。"
        )
    return outline_narrative


def _outline_reminder_text(outlined_at: str | None, world_time_iso: str) -> str:
    """白天 + 大纲很久没更新时返回一句软提醒；否则返回 ``""``（spec 决策 3）。

    纯函数（不碰 PG / agent），只据「大纲上次梳理时刻 ``outlined_at``」与「当前世界
    时刻 ``world_time_iso``」算时间差 + 判此刻是不是 world 活跃时段（白天）：

      * 大纲不旧（差 < :data:`OUTLINE_STALE_HOURS`）→ ``""``（没必要催）。
      * 此刻是深夜（不在 ``[START, END)`` 白天窗口）→ ``""``（夜里不打扰）。
      * 大纲旧 **且** 白天 → 一句「你这份大纲是 X 小时前梳理的、回头看看还准不准、要不要
        用 update_outline 更新」的软提醒文本。

    边界（spec 决策 3 命门）：这**只**算「该不该把提醒纸条递过去」、只控 context 注入；
    world 读完**照样可以不改**——改不改、改成什么全由 world 自决，机制绝不强制它改大纲
    （强制就把刚推翻的「独立环节」换皮请回来了）。``outlined_at`` 为 None（还没有大纲）
    或任一时刻解析不出 → ``""``（无从判旧、保守不催）。
    """
    if outlined_at is None:
        return ""
    outlined = cst_time.parse(outlined_at)
    now = cst_time.parse(world_time_iso)
    if outlined is None or now is None:
        return ""
    elapsed_hours = (now - outlined).total_seconds() / 3600.0
    if elapsed_hours < OUTLINE_STALE_HOURS:
        return ""
    hour = now.astimezone(_CST).hour
    if not (OUTLINE_DAYTIME_START_HOUR <= hour < OUTLINE_DAYTIME_END_HOUR):
        return ""
    return (
        f"（提醒：你这份大纲是大约 {int(elapsed_hours)} 小时前梳理的了，世界这段时间"
        "又往前走了——回头看一眼它还准不准、有没有哪条线该更新「走到哪」或该结掉了。"
        "要不要用 update_outline 改，你自己判断，不改也行。）"
    )


def _materials_section(materials: DailyMaterials) -> str:
    """把当天的外部底料（``briefing``）渲染成喂给 world 的一段「今天的外部底料」公共背景文本。

    底料是世界今天的真实节律（下雨 / 放假影响全家、番剧更新是公开信息）。world **当天
    第一次醒**把它当**公共可得的背景知识**纳入一次（拼进这轮 user 消息、进意识流），
    当天后续轮不再重喂。这里只渲染客观事实，不暗示任何角色去关注 / 行动（赤尾宪法：
    world 不当导演——谁关心番剧是角色性格的事，不在这层决定）。

    调用方（:func:`_run_world_round`）只在「今天有底料且本轮要纳入」时调本函数，所以
    ``materials`` 必非 None —— 「今天没底料（None）」由调用方判定后**整段不拼**（不读
    昨天、不冒充事实），不进这里。

    底料就是抓取 agent 组织好的那段 ``briefing`` 中文话：它已把真实事实整理成连贯背景、
    且对没拿到的源诚实说了「今天没拿到」（降级在 briefing 文本里就说清了，world 直接读
    这段、不需要每源的成功标志）。这里只在 briefing 外裹一句背景定性、把它原样喂给 world。
    """
    return (
        "下面是今天抓到的外部底料，作为全家此刻共处的同一个世界里**公共可得的背景"
        "信息**——天气 / 节假日是客观环境（下雨、放假会影响全家此刻的样子），番剧"
        "更新这类是公开消息（它就摆在那，谁会去关心是各角色性格的事，你只把它当世界"
        "里客观存在的背景，绝不暗示谁去关注或行动）：\n"
        f"{materials.briefing}"
    )


def _roster_section(roster: list[NPCRoster]) -> str:
    """把 NPC 名册渲染成喂给 world 的一段「世界的固定人物」文本，按所属姐妹归类。

    名册是世界里有名有姓的固定 NPC（绫奈 / 赤尾 / 千凪 各自的同学 / 同事 / 闺蜜），
    world **当天第一次醒**把整份名册当**世界里客观存在的人**纳入一次（拼进这轮 user
    消息、进意识流），当天后续轮不再重喂（参考 DailyMaterials 的纳入节奏）。

    按 NPC 的 ``relates_to``（主要关联哪个姐妹的 persona_id）归类——同一姐妹名下的
    NPC 归在一起，每人一行「名字：速写」。归类小标题用 persona_id 本身（akao /
    chinagi / ayana）：哪个 id 对应哪个姐妹由 world 的 system prompt 一处承载（世界
    设定底座），这里不在 scaffolding 文案里硬编任何角色中文名 / 剧情事实（赤尾宪法：
    代码里一个剧情字都不许写，世界谁是谁由 world 从底座读）。NPC 的内容（名字 / 速写
    / 关联谁）全是数据驱动（NPCRoster 表），不是硬编。

    调用方（:func:`_run_world_round`）只在「名册非空且本轮要纳入」时调本函数，所以
    ``roster`` 必非空 —— 「名册为空（还没 seed）」由调用方判定后**整段不拼**，不进这里。

    呈现顺序：``relates_to`` 升序分组、组内按 ``npc_name`` 升序（稳定可读，不靠
    list_npc_roster 的返回序），让同一份名册每次渲染出同一段文本。
    """
    by_sister: dict[str, list[NPCRoster]] = {}
    for npc in roster:
        by_sister.setdefault(npc.relates_to, []).append(npc)

    blocks: list[str] = []
    for sister in sorted(by_sister):
        npcs = sorted(by_sister[sister], key=lambda n: n.npc_name)
        lines = "\n".join(f"  - {n.npc_name}：{n.sketch}" for n in npcs)
        blocks.append(f"与 {sister} 相关的人：\n{lines}")
    body = "\n".join(blocks)

    return (
        "下面是这个世界里有名有姓的固定人物，作为世界里**客观存在的人**——她们各自"
        "有自己的生活，平时不在画面里，但确实存在、随时可能因为自己的事来跟三姐妹中"
        "的某一个发生联系。按主要关联的姐妹归类，每人一句性格底色 + 平时会冒什么事的"
        "速写（你只把她们当世界里客观存在的人，绝不暗示谁此刻一定要出场或行动）：\n"
        f"{body}"
    )


def _sisters_section(states: list[tuple[str, LifeState | None]]) -> str:
    """把三姐妹此刻各自的样子渲染成喂给 world 的一段文本——每人带上**当前状态 + 观测时刻**。

    这是 world 客观叙述对齐的输入面：world 是纯客观世界推演者，它读每个角色此刻在哪 /
    在干嘛，只为让自己的客观叙述跟她对得上（她在上课就别说她在街上）。

    **``observed_at`` 的复活是范围很窄的一次例外（life-idle-wake-via-sense Task 1
    T3 review）**：Task 1（旧范式重设计）收口时曾把这里的 ``next_wake_at``（她想几点
    醒）和 ``observed_at``（状态新旧）整段删除，理由是两者"只服务于判唤醒、而判唤醒
    是自锁源头"（life 一静止就不产 act、world 看世界没动静就判没必要叫、越静越不叫）。
    这个理由现在只对"谁很久没动、world 就该主动挑谁醒"这种**无约束**的判唤醒仍然
    成立——sense 新加的 idle 判断不是那种判唤醒：它有独立、划定清楚的时间外推准则
    （是不是天然的闲时刻 + 睡着不算闲 + 同一静止场景不逐轮重复判，见
    :func:`world_loop_instruction` 里 sense 那段），不会重现旧自锁。但没有
    ``observed_at``，world 手里只有"她此刻在哪"这一份状态、没有时间戳，做 idle
    判断时的"这中间流逝了多久"只能瞎猜。所以这里只加回 ``observed_at`` 这一个字段
    （**不**加回 ``next_wake_at``——那个"她自己排的下次想醒的时间"概念已经整个删除、
    不该复活），专门为了给 idle 判断补一个可外推的时间锚。

    读不到某角色的 LifeState（``None``：她还没活过一轮）时如实写"还没有状态记录"——
    不漏拼、不报错。

    ``states`` 是 ``(persona_id, LifeState | None)`` 列表（调用方按 persona 顺序读好
    传进来）。归类锚点用 persona_id 本身（哪个 id 对应哪个姐妹由 world 的 system
    prompt 一处承载），这里不在 scaffolding 文案里硬编任何角色中文名（赤尾宪法）。
    """
    lines: list[str] = []
    for persona_id, state in states:
        if state is None:
            lines.append(f"- {persona_id}：还没有状态记录（她还没活过一轮）。")
            continue
        lines.append(
            f"- {persona_id}：此刻「{state.current_state}」"
            f"（观测于 {cst_time.to_cst_full(state.observed_at)}）。"
        )
    body = "\n".join(lines)
    return (
        "下面是三姐妹此刻各自的样子——每人此刻在哪、在干嘛、这份状态是什么时候观测到"
        "的。这首先是给你做客观叙述对齐用的（她在上课就别说她在街上）；观测时刻这份"
        "时间戳**只服务一件事**——sense 的 idle 判断需要拿它和现实此刻的时间做外推"
        "（这段时间大概率发生了什么、她大概率还在不在原来那种状态）。除了 idle 判断"
        "这一个例外，你仍然不该用这份时间戳去判断其他任何「谁该不该醒」。\n"
        f"{body}"
    )


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
    detail_written_at: str | None,
    now_iso: str,
    wake_reason: str,
    round_id: str,
    arc_narrative: str | None,
    outline_narrative: str | None,
    sisters_text: str,
    reminder_text: str = "",
    materials_text: str = "",
    roster_text: str = "",
    act_batch_text: str = "",
    end_created_at: str | None = None,
    end_act_id: str | None = None,
) -> list[Message]:
    """把"世界阶段 / 上一版世界叙述 / 现在几点 / 今天的外部底料 / 这批动作"拼成喂给循环的 user 消息。

    ``detail`` 是上一版世界叙述（冷启动时是一句"首次醒来、还没有上一版世界叙述"的
    占位文本）。``now_iso`` 是 engine 算的现实此刻时间（CST）。这里把当前客观 context
    一次喂全，让 world 在循环里推演 + 用 update_world 写新的世界叙述。开头印一行本轮
    标记（``round_id`` + 本批终点游标 ``(end_created_at, end_act_id)``），写回 transcript
    后重投能查重跳过（turn 幂等）、并据终点游标把游标补推到上一轮真正推完的位置。

    ``detail_written_at``：上一版叙述的写入时刻（快照的 ``world_time``，spec 决策
    5b）——「上一版叙述」段标注「这段叙述写于 X」，让续写知道手里这帧画面是什么
    时候画下的，对着现实此刻一步跨过去而不是逐分钟回放（对「叙事落后现实八小时」
    回放循环的釜底抽薪之一）。**必传、无默认值**（同 ``arc_narrative`` 的哲学）：
    冷启动（占位文本、无真实写入时刻）显式传 None、不标注。

    世界设定本身（这家是谁、屋里屋外的空间、三姐妹各自客观作息）由 system prompt
    一处承载，USER 层不再拼——避免世界设定两处真相。这里只喂"此刻动态"：世界阶段 /
    上一版叙述 / 现在几点 / 今天的外部底料 / 唤醒缘由 / 这批动作。

    ``arc_narrative``：最新一版世界阶段的全文（「跨周月仍然成立的世界进展」，
    :func:`_run_world_round` 每轮在反思之后 ``read_world_arc`` 现读传进来）。【世界
    阶段】段**每轮都拼**（与 materials 的"当天一次"不同——世界阶段是世界的慢层底座，
    每轮推演都要看见）：有阶段给全文，None（还没翻过页）给空白说明（:func:`_arc_section`，
    第一版由反思写、不引导续写动手）。**必传、无默认值**：None 是"还没翻过页"的
    显式语义，调用方必须显式给——漏传应当在测试里炸出来（TypeError），而不是静默
    退化成空白说明。

    ``outline_narrative``：最新一版大纲的全文（world 续写自维护的工作记忆——世界此刻
    在走的几条未完成客观线，:func:`_run_world_round` 每轮 ``read_world_outline`` 现读
    传进来）。【大纲】段**每轮都拼**（同世界阶段，是续写沿线推进的朝向）、插在【世界
    阶段】之后、上一版叙述之前：有大纲给全文，None（冷启动还没记过线）由
    :func:`_outline_section` 引导续写用 ``update_outline`` 起头记（区别于世界阶段空白
    时的「不归你动手」——大纲就归续写自己维护）。**必传、无默认值**：None 是"还没有
    大纲"的显式语义，调用方必须显式给（同 ``arc_narrative`` 的哲学）。

    ``reminder_text``：大纲软 reminder 段（spec 决策 3）。由 :func:`_run_world_round`
    据 :func:`_outline_reminder_text` 算出——白天 + 大纲很久没更新时是一句「该回看大纲
    了」的软提醒，否则空串。**非空才插这段**、插在「这一批动作」之前；这只是递一张提
    醒纸条，world 读完照样可以不改（决策 3 命门：只控注入、不强制改大纲）。

    ``materials_text``：当天外部底料渲染出的「今天的外部底料」段（:func:`_materials_section`
    渲染的 briefing）。它是世界今天的真实节律（下雨 / 放假 / 番剧更新），作为**公共
    背景知识**喂给 world。**只在 world 当天第一次醒、有底料且本轮要纳入时**由调用方
    （:func:`_run_world_round`）传非空文本插这段（进意识流一次）；今天没底料 / 当天已
    纳入过时传空串、不插这段（后续轮从 transcript 自然记得，不重喂）。

    ``roster_text``：NPC 名册渲染出的「世界的固定人物」段（:func:`_roster_section`
    渲染、按所属姐妹归类）。它是世界里有名有姓的固定 NPC（同学 / 同事 / 闺蜜），作为
    **世界里客观存在的人**喂给 world。纳入节奏同 materials：**只在 world 当天第一次醒、
    名册非空且本轮要纳入时**传非空文本插这段（进意识流一次）；名册为空 / 当天已纳入过
    时传空串、不插这段（后续轮从 transcript 自然记得，不重喂）。名册与底料是两件独立的
    事、各用各的游标（名册 seed 后总在、底料某天可能没有）。

    ``sisters_text``：三姐妹此刻各自的样子（:func:`_sisters_section` 渲染：拼每人的
    **当前状态 + 观测时刻**）。**必传、无默认值**：拼在【现实此刻】之后，让 world 的
    客观叙述跟她们此刻所处对得上（她在上课就别说她在街上）——这是它的主用途。Task 1
    收口后它不再服务于无约束的「判该不该叫醒谁」（那是自锁源头、已删）；sense 的 idle
    判断是这条规则划定清楚的唯一例外，靠 :func:`_sisters_section` 加回的 ``observed_at``
    做时间外推（见该函数 docstring）。

    ``act_batch_text``：这一批从游标 pull 到的所有人的动作清单（对称 life 读
    mailbox）。非空才插入「这一批动作」段——让 world 看到这段时间攒下的所有动作。
    这段时间没有新 act（纯 self / 心跳推进世界）时留空、不插这段。

    ``end_created_at`` / ``end_act_id``：本批终点游标（落库时刻 + act_id），编进 marker
    供重读命中时推进游标。空批次传 None（marker 编码成 ``end:-``，无终点可推进）。
    """
    materials_section = (
        f"【今天的外部底料】\n{materials_text}\n\n" if materials_text else ""
    )
    roster_section = (
        f"【世界的固定人物】\n{roster_text}\n\n" if roster_text else ""
    )
    # act 批的框架文案：一次拉完后这一批可能横跨几个小时，明示 world 把这段时间
    # 的账一笔收进世界流到此刻的样子、叙述落在【现实此刻】——不按各条旧时间戳
    # 逐条补叙旧场景（coe 实证不明示时模型会锚在旧 act 的时刻逐轮补叙旧戏）。
    # 只是文案语义，不是机制：怎么收、收成什么样仍由 world 推演。
    act_section = (
        "【这一批要你推演客观结果的动作（所有人）】\n"
        "（这批动作可能横跨了一段时间。不用按各条的旧时间戳逐条补叙旧场景——"
        "把这段时间的来龙去脉一笔收进世界流到此刻的样子，叙述落在【现实此刻】。）\n"
        f"{act_batch_text}\n\n"
        if act_batch_text
        else ""
    )
    # 大纲软 reminder 段（spec 决策 3）：非空才插（白天 + 大纲旧时才有），插在「这一批
    # 动作」之前。只控 context 注入、world 读完照样可以不改。
    reminder_section = f"{reminder_text}\n\n" if reminder_text else ""
    # 上一版叙述段带写入时刻标注（spec 决策 5b）；冷启动占位文本无真实写入时刻，
    # 不标注。
    detail_header = (
        f"【你记得的上一版世界叙述】（这段叙述写于 {detail_written_at}）"
        if detail_written_at
        else "【你记得的上一版世界叙述】"
    )
    user_content = (
        f"{_round_marker(round_id, end_created_at=end_created_at, end_act_id=end_act_id)}\n"
        f"{world_loop_instruction()}\n\n"
        f"【现实此刻】{now_iso}\n"
        f"【三姐妹此刻各自的样子】\n{sisters_text}\n\n"
        f"【世界阶段】\n{_arc_section(arc_narrative)}\n\n"
        f"【你的大纲（世界此刻在走的客观线）】\n{_outline_section(outline_narrative)}\n\n"
        f"{detail_header}\n{detail}\n\n"
        f"{materials_section}"
        f"{roster_section}"
        f"【这次醒来的缘由】{wake_reason}\n\n"
        f"{reminder_section}"
        f"{act_section}"
        "看一眼这个世界，推演此刻它什么样，用 update_world 写下来；出现了值得被感知的"
        "客观动静就用 notify 投出去、标清它的客观作用域（谁够得着由在场匹配判，不归你"
        "挑）；最后用 sleep 定下次多久再看。"
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
            # 硬超时包整段（对账 → gate → pull → run/工具副作用 → 收口）：任何一步
            # 挂死（LLM 不返回 / 库查询卡死）都在锁 TTL 之前被掐断、走下面的 fail-open，
            # 绝不留一个挂死的轮占着锁直到 TTL 被下一拍并发——更绝不让 world_tick 永不
            # 返回把同步 await 它的 time source loop 永久堵死（world 永睡）。
            await asyncio.wait_for(
                _run_world_round(tick, lane=lane),
                timeout=WORLD_TICK_TIMEOUT_SECONDS,
            )
    except SingleFlightConflict:
        # heartbeat / self：冗余唤醒，吞掉不抛（log 留痕、不静默）。
        logger.info(
            "[world_tick] %s %s wake hit lock, drop (redundant safety/self wake)",
            lane,
            tick.reason,
        )
        return
    except TimeoutError:
        # 整轮挂死被硬超时掐断：这轮作废、下一拍心跳 / 自排重来（fail-open），
        # 绝不向上抛把 source loop 拖垮。
        logger.error(
            "[world_tick] %s %s round hard-timeout (>%ss), drop this round (fail-open)",
            lane,
            tick.reason,
            WORLD_TICK_TIMEOUT_SECONDS,
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
         act_id)`` 之后**一次拉完**这段时间攒下的 act（按落库顺序）。
         WORLD_ACT_PULL_LIMIT 只是防病态洪峰撑爆单轮 prompt 的防爆栅栏（正常
         永不触发）：命中必打 warning（截了多少、还剩多少）、缘由文本告知 world
         有积压、游标只推进到实际消化的末尾、剩下下轮继续。游标用 created_at
         （单调落库序）不漏 out-of-order act。round_id 批次非空时从**游标起点**稳定
         派生（崩溃扩批仍同 round_id）、空批次从 now 派生。
      5. **turn 幂等查重**：load_session 读已有 transcript，若本轮 round_id 标记
         已在历史里（失败 / 崩溃重读）→ 推进游标到 **marker 记的终点** 后跳过，不再
         run、不重复推演（不是推进到当前批末尾——崩溃+扩批时当前批可能更大）。
      5b. **反思环节（续写之前，双触发）**：第一班「当日尚未反思」
         （``arc_reflected_date`` != 今天，含 None=冷启动）；第二班「当日底料落地
         且尚未被反思消化」（底料存在且 ``arc_materials_reflected_date`` != 今天）
         ——任一命中跑一次无会话的对表反思（同轮命中两个也只跑一次：
         :func:`app.world.reflection.run_arc_reflection`，工具只有 update_arc /
         update_attention、fail-open、成功才落标记——带底料同落两个标记）；续写的
         世界阶段在反思**之后现读**。
      6. 把"上一版世界叙述 / 现在几点 / 这批动作"作为 prompt context 喂入（世界设定
         由 system prompt 一处承载，USER 层不拼），marker 编 round_id + 本批终点游标，
         用**确定性 session_id 续接**跑 agent 工具循环：把 session_id 显式传给
         ``Agent.run(session_id=)``。工具读 ctx 里的 lane + round_id 行动。
      7. 循环自然收口（不再调工具就停）；中途瞬时失败因 max_retries=1 直接抛、
         **游标不推进**（失败不推进、下轮重读这批，act 不丢）。
      8. **收口推演成功才在同一次 WorldState append 里推进游标到本批末尾
         ``(created_at, act_id)`` + 标记今天外部底料已纳入（当天首醒纳入那轮才标）**
         （record_world_round_close）+ 排下次醒（fire_self_wake）——世界叙述快照改由
         update_world 工具在循环里负责写。外部底料**当天第一次醒纳入一次、进意识流**
         （后续轮从 transcript 自然记得、不重喂；今天没底料 / 已纳入则不拼这段）。

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
    # 冷启动 = 没有快照，**或**快照还没有真实世界叙述（detail 空白）。后者是冷启动
    # 反思成功落标后留下的最小占位行（mark_arc_reflected 冷启路径：只承载
    # arc_reflected_date，叙述字段中性空白）——续写若在写首版叙述前崩溃，下一轮读到
    # 的就是它，不能被当成「已有世界叙述」喂模型一段空叙述；仍走冷启动分支（占位
    # detail 文本 + 冷启动缘由、不标注写入时刻）。占位行上的标记 / 游标 / next_wake_at
    # 照常从 snapshot 读（不清掉——占位行存在的意义就是把当日反思标记带过冷启窗口）。
    cold_start = snapshot is None or not snapshot.detail

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
        # WorldState，世界叙述统一由工具落）。占位文本无真实写入时刻，不标注。
        detail = "（首次醒来，还没有上一版世界叙述。）"
        detail_written_at = None
    else:
        detail = snapshot.detail
        # 上一版叙述的写入时刻（spec 决策 5b）：让续写知道手里这帧画面是什么时候
        # 画下的，对着现实此刻一步跨过去而不是逐分钟回放。
        detail_written_at = snapshot.world_time

    # 从游标 pull act（pull 范式核心）：任何唤醒源都从 WorldState 当前 act 游标之后
    # **一次拉完**这段时间攒下的 act（按落库顺序）。WORLD_ACT_PULL_LIMIT 只是防爆
    # 栅栏（正常永不触发，命中见下方探询 + warning；命中时剩下的下轮接着读、不截断
    # 丢弃）。游标为 None（冷启动 / 从没消费过）时读全既有。游标用 created_at
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

    # 防爆栅栏检查（no silent caps）：读满栅栏值时从本批末尾再探一眼还剩多少。
    # 积压正好等于栅栏值（剩余 0）不算命中——行为与全量一致、不告警；真命中（还有
    # 剩）必打 warning 说明截了多少、还剩多少、下轮从游标继续。探询本身也有界
    # （最多再读一个栅栏值，剩余 ≥ 栅栏值时如实报 ">="），且只在病态洪峰才发生，
    # 正常路径零额外查询。
    remaining = 0
    if recent and len(recent) >= WORLD_ACT_PULL_LIMIT:
        overflow = await list_recent_acts(
            lane=lane,
            cursor_created_at=batch_end_created_at,
            cursor_act_id=batch_end_act_id,
            limit=WORLD_ACT_PULL_LIMIT,
        )
        remaining = len(overflow)
        if remaining:
            logger.warning(
                "[world_tick] %s act pull hit fence: consumed %d acts this "
                "round (WORLD_ACT_PULL_LIMIT), %s still queued; cursor "
                "advances only to the consumed end, rest pulled next round",
                lane,
                len(recent),
                (
                    f">={remaining}"
                    if remaining >= WORLD_ACT_PULL_LIMIT
                    else str(remaining)
                ),
            )
    has_backlog = remaining > 0

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

    # 当天外部底料：**当天第一次醒纳入一次、进意识流，之后当天不再重喂**（刀 3 调整）。
    # world 有按天连续 session（意识流 transcript）：纳入那轮把底料写进 user 消息后，当天
    # 后续轮次从 transcript 自然记得，不用每轮重喂。判断纳入与否：
    #
    #   * 按 (lane, **今天 CST**) 读底料（find_daily_materials 只按今天查，绝不读昨天）。
    #   * 今天有底料（非 None）**且** snapshot.materials_ingested_date != 今天（当天还没
    #     纳入过）→ 这轮纳入：渲染 briefing 拼进 user 消息，收口标记 materials_ingested_date
    #     =今天。
    #   * 今天已纳入过（== 今天）或今天还没底料（None）→ 不拼这段、不读昨天、不标记。
    #
    # today 用 now（CST）的 %Y-%m-%d，与 session_id / 收口标记的当天口径一致。
    today = now.strftime("%Y-%m-%d")
    prev_ingested_date = (
        snapshot.materials_ingested_date if snapshot is not None else None
    )
    materials = await find_daily_materials(lane=lane, date=today)
    # 本轮要纳入底料吗：有底料 + 当天还没纳入过。决定 → 渲染拼这段 + 收口标记今天。
    ingest_materials_this_round = (
        materials is not None and prev_ingested_date != today
    )
    materials_text = (
        _materials_section(materials) if ingest_materials_this_round else ""
    )
    # 收口要标记的纳入日期：这轮纳入了就标今天，否则 None（record_world_round_close 收到
    # None 不改 materials_ingested_date、沿用上一版——绝不把已纳入标记清回 None）。
    mark_ingested_date = today if ingest_materials_this_round else None

    # NPC 名册（「世界的固定人物」）：**当天第一次醒纳入一次、进意识流，之后当天不再
    # 重喂**（照 DailyMaterials 套路）。名册是世界里有名有姓的固定 NPC（同学 / 同事 /
    # 闺蜜），world 当天首醒把整份名册按所属姐妹归类拼进 user 消息一次，当天后续轮从
    # transcript 自然记得、不重喂。判断纳入与否：
    #
    #   * 按当前 lane list 名册（list_npc_roster，每个 NPC 取最新一版）。
    #   * 名册非空 **且** snapshot.roster_ingested_date != 今天（当天还没纳入过）→ 这轮
    #     纳入：渲染名册段拼进 user 消息，收口标记 roster_ingested_date=今天。
    #   * 名册为空（还没 seed）或当天已纳入过（== 今天）→ 不拼这段、不标记。
    #
    # 与底料**独立**（各用各的游标、各判各的纳入）：名册 seed 后总在、底料某天可能没
    # 有，两件不相干的事不能共用一个游标互相连累。today 同上（now CST %Y-%m-%d）。
    prev_roster_ingested_date = (
        snapshot.roster_ingested_date if snapshot is not None else None
    )
    # 种子名册的生产自动入口（必改 1）：seed_npc_roster 没有别的生产调用方，不接它
    # 表永远空、首醒 list 永远得空名册、NPC 永不出场。照 persona_chain seed 的「首次
    # 需要时 ensure 一次」先例，把它接在 **world 当天第一次醒、list 之前**——只在
    # 「当天还没纳入过名册」这个首醒分支跑（roster_ingested_date != 今天），当天后续轮
    # 不重 seed（CAS 幂等本就重跑无害，但也别白打一次 DB）。seed 是 CAS 幂等
    # （expected_current_ver=0：只灌一版都没有的 NPC、链非空即已被演化层动过的绝不
    # 盖回出厂速写），先 seed 再 list 保证首醒读得到名册。
    if prev_roster_ingested_date != today:
        await seed_npc_roster(lane=lane)
    roster = await list_npc_roster(lane=lane)
    ingest_roster_this_round = (
        bool(roster) and prev_roster_ingested_date != today
    )
    roster_text = _roster_section(roster) if ingest_roster_this_round else ""
    # 收口要标记的名册纳入日期：这轮纳入了就标今天，否则 None（record_world_round_close
    # 收到 None 不改 roster_ingested_date、沿用上一版——绝不把已纳入标记清回 None）。
    mark_roster_date = today if ingest_roster_this_round else None

    # 反思环节（Task 2b，翻页归它独占）：**续写之前**跑一次无会话的对表反思
    # （独立 AgentConfig、工具只有 update_arc / update_attention、max_retries=1）。
    # 双触发（眼睛闭环）：world 24×7，每天 00:0X 首轮就触发第一班——那时眼睛还没
    # 出门、当天底料不存在，单一标记会让「当天 briefing 永远不被当天反思消化」。
    # 所以分两班：
    #
    #   * 第一班照旧：arc_reflected_date != 今天（含 None=冷启动 / 部署后首跑），
    #     无底料也凭常识对表翻页（现有语义不变）。
    #   * 第二班补班：当日底料存在 且 arc_materials_reflected_date != 今天——白天
    #     底料落地后再消化一次（眼睛带着旧关注看到的结果就在底料里）。
    #
    # 两个条件命中任一就跑、同轮命中两个也**只跑一次**（一次带底料的反思已覆盖
    # 两班职责——比如午后部署时首轮就带底料）。落哪些标记由 run_arc_reflection 按
    # 「本次是否带底料」决定：带底料同落两个、无底料只落第一班标记。反思标记独立
    # 于底料 ingest 标记（spec 决策 5：续写成功不代表反思成功）；反思**成功**才落
    # （在 run_arc_reflection 里 mark_arc_reflected），失败不落 → 同日后续轮自动
    # 重试。fail-open：run_arc_reflection 绝不抛——失败只记 error 日志、当轮续写
    # 照常。
    prev_reflected_date = (
        snapshot.arc_reflected_date if snapshot is not None else None
    )
    prev_materials_reflected_date = (
        snapshot.arc_materials_reflected_date if snapshot is not None else None
    )
    needs_daily_reflection = prev_reflected_date != today
    needs_materials_reflection = (
        materials is not None and prev_materials_reflected_date != today
    )
    if needs_daily_reflection or needs_materials_reflection:
        await run_arc_reflection(
            lane=lane,
            now=now,
            snapshot=snapshot,
            materials=materials,
            round_id=round_id,
            trace_session_id=session_id,
        )

    # 世界阶段（慢层）：每轮都读最新一版喂进推演输入——「跨周月仍然成立的世界进展」
    # 是每轮推演的底座，与 materials 的"当天纳入一次"不同。还没翻过页（None）时
    # 由 _arc_section 如实说明空白（第一版由反思写、续写无手碰世界阶段）。**必须在
    # 反思之后现读**（spec 决策 5）：update_arc 已 durable 落库而反思 Agent 随后失败
    # 时，续写也要读到新的世界阶段——绝不能用反思前缓存的值。
    arc = await read_world_arc(lane=lane)
    arc_narrative = arc.narrative if arc is not None else None

    # 大纲（world 续写自维护的工作记忆，spec task2）：每轮读最新一版当朝向喂进推演——
    # world 沿着每条客观线把在跑的进程往前推到出结果（绫奈急诊→候诊→出结果），不再
    # 走一步看一步失忆推演。**用本轮 tick 的 lane**（泳道隔离命门同 WorldState / WorldArc，
    # coe / ppe 绝不读到别的泳道的大纲）。还没记过线（None=冷启动）由 _outline_section
    # 引导续写用 update_outline 起头记。
    #
    # 大纲软 reminder（spec 决策 3）：据大纲上次梳理时刻 outlined_at 与现实此刻 now_iso
    # （world_time 跟现实走、由 update_outline 自填现实 CST）算时间差 + 判白天，白天 +
    # 大纲很久没更新时算出一句软提醒注入 context。没有大纲（None）→ outlined_at 传 None
    # → 不催（_outline_reminder_text 内部判 None 即返 ""）。这只控注入、不强制 world 改。
    outline = await read_world_outline(lane=lane)
    outline_narrative = outline.narrative if outline is not None else None
    reminder_text = _outline_reminder_text(
        outline.outlined_at if outline is not None else None, now_iso
    )

    # 三姐妹此刻各自的样子（客观叙述对齐的输入面）：每轮读每个 persona 的 LifeState
    # 快照，把**当前状态 + 观测时刻**喂进 USER 消息，让 world 的客观叙述跟她们此刻所处
    # 对得上（她在上课就别说她在街上）。这里不读它判「谁状态停滞太久、world 该主动挑谁
    # 醒」——那种无约束的判唤醒仍是自锁源头（Task 1 收口已删、没有复活）；观测时刻这个
    # 字段唯一服务 sense 新增的 idle 判断做时间外推（划定清楚的例外，见
    # _sisters_section docstring）。**用本轮 tick 的 lane**（不是进程默认 lane）：泳道
    # 隔离命门同 WorldState / WorldArc，coe / ppe 绝不能读到别的泳道的"她此刻状态"。
    # 直读 domain 层 find_life_state（拿 current_state / observed_at 字段），不复用只读
    # 进程 lane、会丢字段的 memory.context 内部组装（spec 决策 3）。读不到某 persona 的
    # 快照（None：她还没活过一轮）由 _sisters_section 如实降级（不漏拼、不报错）。
    persona_ids = await list_all_persona_ids()
    sister_states = [
        (pid, await find_life_state(lane=lane, persona_id=pid))
        for pid in persona_ids
    ]
    sisters_text = _sisters_section(sister_states)

    messages = _world_loop_messages(
        detail=detail,
        detail_written_at=detail_written_at,
        now_iso=now_iso,
        wake_reason=wake_reason,
        round_id=round_id,
        arc_narrative=arc_narrative,
        outline_narrative=outline_narrative,
        sisters_text=sisters_text,
        reminder_text=reminder_text,
        materials_text=materials_text,
        roster_text=roster_text,
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

    # 推演成功收口：在**同一次** WorldState append 里推进游标 + 标记底料 / 名册已纳入
    # 今天（record_world_round_close）。失败时上面的 run 已抛、不会走到这里，游标不推进、
    # materials_ingested_date / roster_ingested_date 不被误标记，下轮重读这批 act + 重新
    # 纳入底料 + 名册（都不丢）。
    #
    #   * 游标：非空批传本批末尾 ``(created_at, act_id)``；空批次传 None（没读到 act 没什么
    #     可推进，游标沿用上一版）。游标用 created_at（落库序）不漏。
    #   * 底料：这轮纳入了传今天日期（标成今天）；没纳入（已纳入过 / 今天没底料）传 None
    #     （不改、沿用上一版已有标记，绝不清回 None）。
    #   * 名册：这轮纳入了传今天日期；没纳入（已纳入过 / 名册为空）传 None（同底料语义，
    #     名册与底料各用各的游标、互不打架）。
    #
    # 几块并进一次 append：空批次但当天首醒纳入了底料 / 名册时，游标传 None 不推进、但
    # materials_ingested_date / roster_ingested_date 仍能各标成今天（一轮一版、不冲突、原子）。
    advance_cursor_to = (
        (batch_end_created_at, batch_end_act_id)
        if batch_end_created_at is not None and batch_end_act_id is not None
        else None
    )
    await record_world_round_close(
        lane=lane,
        advance_cursor_to=advance_cursor_to,
        materials_ingested_date=mark_ingested_date,
        roster_ingested_date=mark_roster_date,
    )

    # transcript 沉淀折叠（沉淀 Task 2，spec 决策 4/5）：本轮写回已在 Agent.run 里
    # durable 落定（两阶段解耦），这里在同一串行窗口（仍在 actor 锁内）做其后的独立
    # 折叠步骤——达到阈值就把整卷压成推演者口吻的当天梗概 + marker 保全行（终点
    # 游标随 marker 原样保全，游标补推不受折叠影响）。位置在收口（推进游标 / 标
    # 底料）之后、**排下次醒之前**（codex T3 必改 1）：沉淀是一次离线 LLM 调用
    # （最长 120s，硬超时见 app.agent.sediment），全程占着 actor 单飞锁——若先排
    # self-wake，短 sleep（最短 60s）的自排会在折叠期间到达撞锁被吞（world 的
    # 锁冲突一律吞掉、无 reschedule 保底）；fold 完成后才开始给下一轮自排计时，
    # 撞锁窗口消失。fold_session 整段 fail-open（绝不抛），失败本版不折、下轮
    # 再试。成本入账在沉淀回调内自带独立 collect_usage 作用域（actor =
    # "world:sediment"），在上面本轮 collect_usage 之外调用——绝不混进 world 本体
    # 已落账的 usage。
    await fold_session(
        session_id,
        build_world_fold_policy(lane=lane, session_id=session_id, round_id=round_id),
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
