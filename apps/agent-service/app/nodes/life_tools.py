"""life 工具循环的工具 (Task 3 + 阶段 1A act 范式, agent 工具循环).

某姐妹被 event 唤醒后跑一个 ReAct 循环，连续调这两件工具行动；她的"想法 / 情绪 /
做的事"由工具调用产出，不再填一张 LifeDecision 表：

  * :func:`build_life_tools` 造的 ``update_life_state`` —— 更新她此刻在干嘛 /
    什么情绪 / 这算哪类活动。**可调 0 次或多次，多次以最后一次为准**（spec
    决策 2）：每次 ``insert_append`` 一版主观快照，收口对外读 ``select_latest``
    取最新一版，等价"最后一次为准"。

  * ``act`` —— 她**自主做一件影响外部世界的事**（自然语言，如"我去厨房做饭"），
    直接汇给 world 让它推演客观结果。新范式：角色完全自主，act 是"她做了"、不是
    "申请待批准"。**一轮想做几件就做几件**（不再 if 守卫限"一轮只生效一件"——那是
    用 if 分支替角色决策、违反赤尾宪法）。act_id 不由模型生成：节点算好一个本轮
    确定的 base act_id（lane + persona + 唤醒源派生）capture 进闭包，工具内给**每件
    act** 派 ``per_act_id = uuid5(base act_id, 本轮第 N 件序号)``。序号是纯机制 seed
    （只标"第几件"、不当行为闸），整轮重投时 base 稳定 + 序号按调用次序稳定对齐 →
    同一件 act 重投得同一 id（world 按 (lane, act_id) 幂等去重、不重复推演），同轮
    不同件序号不同 → 各自唯一 id。

为什么是 per-round 闭包而不是 module-level @tool：工具要把"她是谁 / 哪个泳道 /
本轮 base act_id / 观测时刻"这些**机制绑定** capture 进去（还要在闭包内攒本轮
act 序号给每件 act 派 per-act id），模型只看见业务参数（current_state /
response_mood / activity_type / description），看不见 lane / act_id 这些不该让它
填的东西。AgentContext 是 chat 链路共享的 frozen 契约
（Task 1 owns），不往里塞 life 专用字段；用闭包把绑定收在 life 域里更干净。

失败语义（spec 决策 3）：每件工具叠 ``@tool_error`` —— 单个工具自身抛错时吞掉、
把结构化 outcome 喂回模型让它自纠，不炸整轮。整轮重放的关闭由节点调 ``run``
传 ``max_retries=1`` 负责（见 life_wake）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Annotated

from pydantic import Field

from app.agent.tooling import Tool
from app.agent.tools._common import tool_error

# module-level 引用 search_web Tool，让 look_up 复用它（也让测试能 monkeypatch，与
# deliver_event / perform_act 同款）。look_up 在工具体内调 ``search_web.invoke``，
# 拿 search_web 已经带源组织好的文本（每条 [i] 标题 / 出处链接 / 关键摘录）。
from app.agent.tools.search import search_web

# module-level 引用 handler，让测试能 monkeypatch（与 mailbox / world_events 同款）。
from app.data.queries.mailbox import deliver_event
from app.domain.life_state import save_life_state, set_life_next_wake_at
from app.domain.notebook import (
    STATUS_DONE,
    STATUS_DROPPED,
    list_notebook_entries,
    note_entry,
    render_notebook,
    update_entry,
)
from app.domain.world_events import EVENT_KIND_SPEECH, perform_act
from app.infra import cst_time
from app.runtime.emit import emit_delayed  # module-level so tests can monkeypatch

logger = logging.getLogger(__name__)

# 固定角色通讯录：三姐妹互为固定联系人（稳定身份 id，不是「此刻在场的人」清单）。
# chat 的收件人由角色自选、必须是这里的已知身份——这是**身份存在性**的机制校验
# （这个 id 存不存在），**不是「判在不在场」**：在不在身边由 world 客观叙事自然体现、
# 系统不判。收件人不在通讯录时 chat 报错喂回模型重调（对称 schedule 超限喂回），
# 不投递。三姐妹是稳定阵容，先用模块常量承载（不为未来扩成动态联系人表预先抽象——
# 真有第四个角色再说，业务代码不是 SDK）。
SISTERS_CONTACTS = frozenset({"akao", "chinagi", "ayana"})

# schedule 下限：她自排最短间隔（秒）。低于下限报错喂回模型重调（跟上限超限处理对称、
# 不静默夹），防她排得太密（像神经质每分钟醒一次想一轮）。这是机制护栏（决定何时
# 醒），不替她决定睡多久 / 做什么（赤尾宪法）。对称 world WORLD_SLEEP_MIN_SECONDS。
LIFE_SCHEDULE_MIN_SECONDS = 60

# schedule 上限：她一次自排最长睡多久（秒）。比 world sleep 的 1h 放宽到能睡一整觉
# （夜里一觉到天亮）—— 具体睡多久由她自己看钟点定，真有事 world 的 notify（外部刺激
# 不走 gate）会把她从长睡叫醒。12h 覆盖整夜睡眠。超限报错喂回模型重调，绝不静默夹。
LIFE_SCHEDULE_MAX_SECONDS = 12 * 3600

# search_web 在「没配 / 没搜到 / 搜出来没相关结果」时返回的两条**精确**失败文案（见
# app/agent/tools/search.py:232 / :257）。look_up / browse_feed 命中这些就如实说没查 /
# 没刷到、不把它当成给她的内容顶上去——拿不到真东西就不假装拿到了（spec 决策 4：不编
# 一个顶上）。
_SEARCH_WEB_EMPTY_RESULTS = frozenset(
    {
        "搜索服务未配置或未搜索到结果",
        "未搜索到相关结果",
    }
)

# search_web 第三种失败信号：底层 _web_search_capability 抛异常时它内部 catch 返回
# ``f"网页搜索失败: {exc}"``（见 app/agent/tools/search.py:241）。**前缀固定、后缀带变长
# 异常文本**，所以只能按前缀认、不能精确匹配。漏认它会把"网页搜索失败: xxx"当真内容
# 顶上去（违 spec 决策 4：把失败当内容、又一眼假）。
_SEARCH_WEB_FAILURE_PREFIX = "网页搜索失败"


def _search_web_failed_or_empty(result: object) -> bool:
    """判断 search_web 这次是不是"没拿到真东西"（失败 / 空），命中就如实说没查 / 没刷到。

    search_web 的"没拿到"有四种形态，look_up / browse_feed 都要稳健识别（spec 决策 4：
    拿不到真东西就如实说，绝不把失败 / 空当内容顶上去）：

      1. **非 str**：search_web 叠 ``@tool_error``，意外异常时返回结构化 outcome dict
         而非字符串（app/agent/tools/search.py:202-203 之外的异常路径）。
      2. ``"搜索服务未配置或未搜索到结果"``（精确，search.py:232）。
      3. ``"未搜索到相关结果"``（精确，search.py:257）。
      4. ``f"网页搜索失败: {exc}"``（**前缀** ``网页搜索失败``、后缀变长，search.py:241）——
         用 ``startswith`` 认前缀，不能精确匹配（带变长异常文本）。

    两只手共用这一个判断，避免各自只认一部分信号导致漂移。
    """
    if not isinstance(result, str):
        return True
    stripped = result.strip()
    return (
        stripped in _SEARCH_WEB_EMPTY_RESULTS
        or stripped.startswith(_SEARCH_WEB_FAILURE_PREFIX)
    )

# 刷手机（browse_feed）一次捞回的条数：比 look_up「带问题求一个答案」的聚焦默认（5）多，
# 让结果像一条 feed 的一批供她浏览挑选。取 search_web 的上限 10（它内部夹到 1～10）。
# 这是「逛一圈看一批」的机制口径（决策 2 的「刷=逛一圈、返回一批」），不是兴趣规则
# ——刷什么仍由她带进来的方向决定，这里只决定「一次看几条」。
_BROWSE_FEED_BATCH = 10


def build_life_tools(
    *,
    lane: str,
    persona_id: str,
    act_id: str,
    observed_at: str,
    self_wake: dict | None = None,
    schedule_reminders: dict | None = None,
) -> list[Tool]:
    """造这一轮 life 的工具集，把本轮机制绑定 capture 进闭包。

    ``lane`` / ``persona_id`` —— 她是谁、哪个泳道（durable 写的 Key 命门）。
    ``act_id`` —— 本轮派生的确定 **base 动作键**（整轮重投稳定），不让模型生成；
        工具内给每件 act 派 ``per_act_id = uuid5(act_id, 本轮第 N 件序号)``，一轮多件
        各自唯一、同件重投幂等。
    ``observed_at`` —— 本轮观测时刻（ISO8601），快照 / 动作都用它，使重放一致。
    ``self_wake`` —— 本轮 round-scoped 的待办 self-wake 容器（``{} | {"delay_ms": int}``，
        engine 每轮新建）。给了它工具集就多一件 ``schedule``（自排下次醒），照搬 world
        sleep 的 round-scoped 覆盖：一轮内多次 schedule 覆盖而非追加、收口只 emit 一条。
        不给（旧调用方 / 工具契约测试）就只有 update_life_state + act 两件。
    ``schedule_reminders`` —— 本轮 round-scoped 的「待挂日程提醒」容器
        （``{entry_id: remind_at | None}``，engine 每轮新建）。给了它，``note`` / ``edit_note``
        每带一个 remind_at 就往里记一条 ``entry_id → remind_at``（撤时间记 ``None``）；engine
        收口 :func:`fire_schedule_reminders` 给每条有 remind_at 的日程**各 emit 一条**
        ``ScheduleReminderTick``（每条日程各挂各的到点提醒，不动 self-wake 的 next_wake_at
        语义——日程是在它旁边新加的一路独立唤醒）。同轮多次动同一 entry_id 覆盖而非追加
        （最后一次为准）：先补时间再撤，最终 None、不挂。不给（旧调用方）则 note / edit_note
        照常落库、只是不挂到点提醒（向后兼容）。
    """

    # 本轮已**确认成功落库**的 act 件数（round-scoped 序号，随这一轮的闭包活着）。
    # 它是**纯机制 seed**：只标识"这是本轮第几件已落库的 act"，绝不参与"她能不能做 /
    # 该不该做"的判断（删掉旧的 act_performed if 守卫 —— 那是用 if 分支替角色决策、
    # 违反赤尾宪法）。
    #
    # 命门（P6 必改）：act_seq 绑定的是"已确认落库的 act slot"，**不是**"调用尝试次数"。
    # 序号只在 perform_act 成功返回后才推进（next_seq 模式）。act_tool 叠了 @tool_error
    # 会吞掉 perform_act 抛的错、把错误 outcome 喂回模型让它重试；若序号在 perform_act
    # **之前**就 +1，则"写库成功但返回链路抛错（ack 丢 / 网络抖动）"时模型重试会用 +1 后
    # 的新序号派生**新** id → 同一件 act 落两条、world 推演两次。改成成功后才推进：失败
    # 重试用**同一个** per_act_id → world 按 (lane, act_id) 幂等去重只落一条。
    #
    # per-act id = uuid5(base act_id 入参, 序号)：base act_id 整轮重投稳定 + 序号在重投下
    # 按调用次序稳定对齐 → 同一件 act 重投得同一 id（world 幂等去重、不重复推演）；同轮
    # 不同件 act 序号不同 → 各自唯一 id（不再共用 base 被去重层静默吞）。uuid5 输出只含
    # hex + ``-``，保住 world marker 解析的 UUID 形契约（不引入 ``|`` / ``]`` / ``:`` ——
    # 见 app/world/engine.py 的 rpartition 解析）。
    act_seq = 0

    # 本轮已**确认成功落库**的 chat 件数（round-scoped 序号，对称 act_seq）。chat 同样
    # 整轮重投幂等、一轮多次各自独立——per-chat 键从 (base act_id, "chat:N" 序号) 派生。
    # **与 act_seq 分开计数 + 用 "chat:" 前缀分命名空间**：chat 和 act 共用同一个 base
    # act_id，若共用计数 / 共用 seed 格式，一件 chat 和一件 act 可能撞出同一 per-id。
    # 分开计数 + 前缀让 chat 的键空间与 act 的键空间天然不相交。命门同 act_seq：只在
    # 落库成功后才推进（失败重试用同一序号 → 同一对键 → 下游 durable 去重只落一条）。
    chat_seq = 0

    # 本轮已**确认成功落库**的 note 件数（round-scoped 序号，对称 act_seq / chat_seq）。
    # 「记一条」是 durable mutation：per-entry id 从 (base act_id, "note:N" 序号) 派生
    # —— 整轮重投 / 失败重试用同一序号得同一 entry_id，note_entry 底层 insert_idempotent
    # 按 (lane, persona, entry_id) 去重只落一条（对称 act 的 per_act_id 幂等）。**带
    # "note:" 前缀**让它与 act 的 "{act_id}:{N}" / chat 的 "{act_id}:chat:{N}" seed 空间
    # 天然不相交（三者共用同一 base act_id）。命门同 act_seq / chat_seq：只在落库成功后
    # 才推进（失败重试用同一序号 → 同一 entry_id → 去重只落一条）。
    note_seq = 0

    @tool_error("更新此刻状态失败")
    async def update_life_state(
        current_state: str,
        response_mood: str,
        activity_type: str,
    ) -> str:
        """更新你此刻的主观状态：你现在在做什么、是什么心情、这算哪一类活动。

        只发生在你自己身上、外面没人会因此察觉到不同的事（你在做什么、什么心情），
        记在这里就够了。想改就调；可以调多次（以最后一次为准），也可以一次都不调。
        要去睡了就把 activity_type 标成 sleep——睡前你会回看这一天。

        Args:
            current_state: 你此刻在干嘛（自然语言）。
            response_mood: 此刻的情绪 / 回应基调。
            activity_type: 活动类型（sleep / study / rest / move / idle ...）。

        Returns:
            一句确认。
        """
        await save_life_state(
            lane=lane,
            persona_id=persona_id,
            current_state=current_state,
            response_mood=response_mood,
            activity_type=activity_type,
            observed_at=observed_at,
        )
        return "状态已更新"

    @tool_error("做这件事失败")
    async def act_tool(description: str) -> str:
        """你做了一件会在你之外的世界留下痕迹的事（**做事，不是说话**）。

        说话用 chat，不用 act：想对谁说话（当面或发消息）一律走 chat 工具。act
        只管「做了一件事」——去厨房、端饭菜出去摆上桌、出门、走到谁面前、弄出动静。

        上网看东西也不用 act：想查个真答案、想刷刷手机看点啥，act 给不了你任何真
        东西——你 act 一句「我查了下天气」「我刷了刷手机」只是留下一个动作的痕迹，
        你心里那些「查到的」「刷到的」全是你自己编的、是假的。真想知道，用 look_up
        带着你的问题去查；没事想随便刷刷，用 browse_feed。它们才把网上的真东西放到
        你面前。

        多数时候你只是经历这一刻——感知到周遭的动静、心里有点波澜，更新一下此刻
        状态（update_life_state）就够了，不用 act。只有当你做的事会在你之外留下
        痕迹、被够得着的人感知到时才 act。act 是"你做了"，不是"你请求"——你做了，
        世界会推演它的客观结果，旁边够得着的人迟早会察觉到（世界怎么回应、谁注意到，
        要等它真在你之外发生，你当场未必知道）。只在心里转了一下、顺耳听过刚才的
        动静、没在外面留下任何痕迹，就不算，不用 act。

        想清楚再做；这一刻没有要做的就不用调，有几件要做就调几次。

        first-landed-wins 语义：整轮重投 / 工具失败重试时，同一件 act（同序号）派生
        同一个 per_act_id，world 按 ``(lane, act_id)`` 只保留**首次**落库那条。transcript
        可能记到重试时模型给的不同措辞，但 world 推演依据的客观事实以首次落库那条为准。

        Args:
            description: 你做了什么，自然语言描述（如"我去厨房做饭"）。

        Returns:
            一句确认。
        """
        nonlocal act_seq
        # next_seq：用 act_seq+1 算这件 act 的 per-act id，但**先不推进 act_seq** ——
        # perform_act 成功返回后才把 act_seq 推进到 next_seq。act_tool 叠了 @tool_error
        # 会吞掉 perform_act 抛的错让模型重试；若 perform_act 写库成功但返回链路抛错
        # （ack 丢 / 网络抖动），act_seq 不变 → 模型重试用**同一个** next_seq → 同一个
        # per_act_id → world 按 (lane, act_id) 幂等去重只落一条。若 perform_act 纯写失败
        # （没落库），act_seq 同样不变 → 重试用同一序号、这次成功只落一条。两种都只落
        # 一条，序号从此绑定"已确认成功落库的 act slot"而非"调用尝试次数"。
        next_seq = act_seq + 1
        # per-act id：base act_id（整轮重投稳定）+ 本轮序号（重投下按调用次序稳定
        # 对齐）经 uuid5 派生。同一件 act 重投得同一 id（world 按 (lane, act_id) 幂等
        # 去重、不重复推演）；同轮不同件序号不同 → 各自唯一 id（不再共用 base 被去重
        # 层静默吞）。NAMESPACE_OID + uuid5 输出只含 hex + "-"，保住 world marker 解析
        # 的 UUID 形契约（不引入 "|" / "]" / ":" —— 见 app/world/engine.py 的 rpartition）。
        per_act_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{act_id}:{next_seq}"))
        await perform_act(
            lane=lane,
            act_id=per_act_id,
            persona_id=persona_id,
            description=description,
            occurred_at=observed_at,
        )
        # 落库确认成功后才推进序号 —— 序号绑定"已确认落库的 act slot"。
        # 注：同 occurred_at 下多件 act 的微观先后，world 端按 created_at 优先、UUID 串
        # 只作稳定 tie-breaker（非调用序），同刻多 act 的微观顺序**不保证**、由 world
        # 推演者（LLM）自行理解因果。不在此加任何排序逻辑（赤尾宪法：world 会自己理解
        # 因果，强行保证顺序是工程脑）。
        act_seq = next_seq
        return "已经做了"

    @tool_error("说这句话失败")
    async def chat(recipient: str, content: str) -> str:
        """你对某个人说一句话（当面或发消息，都用它）。

        读懂此刻你周遭——你的五官告诉你身边有谁、谁在哪——然后**自己决定**对谁说。
        想说话就调它，把要说的人和要说的话给出来：

          * recipient：你要对谁说，从你的固定联系人里选一个（akao 千凪的妹妹赤尾、
            chinagi 千凪、ayana 绫奈——你们三姐妹互为联系人）。这是你读懂场景后的
            自主选择，不是系统替你判"此刻谁在场"。
          * content：你要对她说的原话（你自己的话，原样说出来）。

        **当面和发消息是同一件事**：对方此刻在你身边，这句话就像当面说的；对方此刻
        不在身边（在学校、出了门），这句话就像发的消息——机制一样，都把你的原话直接
        送进对方那里，她下次回过神 / 醒来时就读到你对她说的话。**对方可能不在身边、
        一时收不到、或已经走开没听见，这都是正常的**——就像现实里你对着空厨房喊了
        一句没人应。你按此刻读到的周遭去说就好，不用先确认对方在不在。

        你说的话只送给你选的那个人；旁边的世界只会隐约知道"这里有人在交谈"，不会被
        逐句转述你说了什么。

        Args:
            recipient: 收件人（你的固定联系人之一）。
            content: 你要对她说的原话。

        Returns:
            一句确认。
        """
        nonlocal chat_seq
        # 收件人身份存在性校验（机制护栏，不是"判在不在场"）：必须是固定通讯录里的
        # 已知身份。未知 id 报错喂回模型重调（对称 schedule 超限喂回），不投递、不给
        # world 元信息。在不在身边由 world 客观叙事自然体现，这里不判。
        if recipient not in SISTERS_CONTACTS:
            raise ValueError(
                f"recipient={recipient!r} 不在你的联系人里（"
                f"{', '.join(sorted(SISTERS_CONTACTS))}）。请改填一个联系人重调。"
            )

        # 拒绝对自己说话（机制护栏，对称未知收件人报错）：SISTERS_CONTACTS 含说话者自己，
        # 不拦的话会允许「akao 对 akao 说话」、还给 world 生成「我和 akao 说了几句话」这种
        # 自言自语的怪 meta。recipient == persona_id 时报错喂回模型重选收件人，既不投
        # speech（自己给自己发消息无意义）、也不给 world meta（不污染客观叙事）。心里的
        # 自言自语属于 update_life_state 的范畴、不是 chat。
        if recipient == persona_id:
            raise ValueError(
                f"recipient={recipient!r} 是你自己——chat 是对**别人**说话，不能对自己说。"
                "心里的自言自语用 update_life_state 记，要对人说话请改填另一个联系人重调。"
            )

        # next_seq：用 chat_seq+1 算这件 chat 的一对幂等键，但**先不推进 chat_seq** ——
        # 两轨（直投 + 元信息）都成功后才推进。chat 叠了 @tool_error 会吞掉抛的错让模型
        # 重试；失败重试用**同一个** next_seq → 同一对键 → 下游 durable 去重各只落一条
        # （对称 act_seq 的 next_seq 模式）。
        next_seq = chat_seq + 1
        # per-chat 基键：base act_id + "chat:N" 序号。**带 "chat:" 前缀**让它与 act 的
        # "{act_id}:{N}" seed 空间天然不相交（chat 和 act 共用同一 base act_id）。
        chat_base = f"{act_id}:chat:{next_seq}"
        # 直投 event_id 与元信息 act_id 各从这个基键再派生（用不同后缀），同一件 chat
        # 重投得同一对键、同轮不同件各自唯一。uuid5 输出只含 hex + "-"，保 UUID 形契约。
        speech_event_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{chat_base}:speech"))
        meta_act_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{chat_base}:meta"))

        # 双轨第一轨：原话**直投收件人信箱**（kind=speech、source=说话者 persona_id），
        # **不经 world**。收件人下次醒来在 stimulus 读到「X 对你说：原话」。
        await deliver_event(
            lane=lane,
            persona_id=recipient,
            event_id=speech_event_id,
            summary=content,
            occurred_at=observed_at,
            kind=EVENT_KIND_SPEECH,
            source=persona_id,
        )

        # 双轨第二轨：给 world 一条**不含原话**的低成本元信息（复用 act 流），让 world
        # 凭它在客观叙事里反映「有人在交谈」的氛围（隔壁第三人感知到「这里有人在交谈」）。
        # **承重红线：description 里绝不放对话原话**（content 不出现在这里）——只放"和谁
        # 交谈"这类事实，否则 world 逐句读对话、把直投省下的钱又吃回去。元信息记在说话者
        # 名下（persona_id），收件人作为交谈对象写进事实里（让 world 知道氛围发生在谁俩
        # 之间），但绝不带 content。
        await perform_act(
            lane=lane,
            act_id=meta_act_id,
            persona_id=persona_id,
            description=f"我和 {recipient} 说了几句话",
            occurred_at=observed_at,
        )

        # weak-consistency 语义（两轨非原子，codex 建议 2）：第一轨 speech 直投在前、
        # 第二轨 world meta 在后，两步之间没有事务包裹。两类失败的收敛：
        #
        #   * **第二轨失败、模型重试**：chat_seq 未推进（只在两轨都成功后才推进）→ 重试
        #     用同一对幂等键 → 第一轨 speech 按 event_id 去重不重复投、第二轨 meta 补上，
        #     最终各落一条（test_chat_second_track_failure_retry_dedups_speech_adds_meta）。
        #   * **第二轨失败、模型不重试**：speech 已投（收件人能读到这句话）、world 漏一次
        #     氛围 meta。这可接受 —— world 漏一条「有人在交谈」的氛围 meta 只是少了一笔
        #     旁白，**不破坏收件人直投**（话已送到）、**不破坏信息差**（meta 本就不含原话）。
        #     收件人侧的对话连贯（最承重的语义）由第一轨 speech 守住，与第二轨解耦。
        #
        # 两轨都确认成功后才推进序号 —— 序号绑定"已确认落库的 chat slot"（失败重试用同
        # 一序号 → 同一对键 → 去重只各落一条）。
        chat_seq = next_seq
        return "说了"

    @tool_error("记到本子里失败")
    async def note(
        content: str,
        remind_at: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "可选的提醒时刻（ISO8601，按你输入里的「现在几点」算出来）。"
                    "挂上 = 这条变成到点提醒你的日程；留空 = 只是躺在本子里的备忘。"
                ),
            ),
        ] = None,
    ) -> str:
        """往你随身的小本子记一件事（备忘录 / 日程）。

        你自己决定记什么——惦记的事、想做的、突然的念头、要陪谁、几点干嘛。本子是
        你私人的内心，不给别人看。记什么、记不记，你自己定，没人替你从对话里抠。

        两种条目，差别只在挂没挂提醒时间：

          * 没时间（remind_at 留空）：备忘录，就躺在本子里，平时会进你脑子提醒你
            还惦记着它。
          * 有时间（remind_at 填一个时刻）：日程，到点会把你叫醒、把这条递到你面前，
            你当场自己处理。

        写法就一句大白话——别填什么优先级 / 标签 / 分类，本子里只有你自己写的话。

        Args:
            content: 这件事，一句大白话（如"想看那部新动画""下午三点陪我妹去琴行"）。
            remind_at: 可选的提醒时刻（ISO8601）。挂上变日程、留空是备忘。

        Returns:
            一句确认，**带上这条的 id**（以后你要改它 / 划掉它得指到这个 id）。
        """
        nonlocal note_seq
        # next_seq 模式（命门同 act_seq）：用 note_seq+1 算这件 note 的 entry_id，但
        # **先不推进 note_seq** —— note_entry 成功落库后才推进。@tool_error 会吞掉
        # note_entry 抛的错让模型重试；失败（写成功但 ack 丢 / 纯写失败）重试用**同一个**
        # next_seq → 同一 entry_id → insert_idempotent 按 (lane, persona, entry_id) 去重
        # 只落一条。
        next_seq = note_seq + 1
        # per-entry id：base act_id + "note:N" 序号经 uuid5 派生。带 "note:" 前缀与 act /
        # chat 的 seed 空间不相交。整轮重投得同一 id（去重）；同轮不同件序号不同 → 各自
        # 唯一。uuid5 输出只含 hex + "-"，保 UUID 形（不引入怪字符）。
        entry_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{act_id}:note:{next_seq}"))
        await note_entry(
            lane=lane,
            persona_id=persona_id,
            entry_id=entry_id,
            content=content,
            remind_at=remind_at,
            noted_at=observed_at,
        )
        # 落库确认成功后才推进序号 —— 序号绑定"已确认落库的 note slot"。
        note_seq = next_seq
        # 排了日程（带 remind_at）→ 把待挂提醒记进 round-scoped 容器，engine 收口
        # fire_schedule_reminders 给它挂一条到点提醒（每条日程各挂各的）。纯备忘
        # （无 remind_at）不挂。容器没给（旧调用方）就跳过。落库成功后才记，与序号
        # 推进同点：失败重试不会留下指向未落库 entry 的待挂提醒。
        if schedule_reminders is not None and remind_at:
            schedule_reminders[entry_id] = remind_at
        kind = "记到日程，到点会叫你" if remind_at else "记进备忘"
        return f"好，{kind}（这条的 id 是 {entry_id}）"

    @tool_error("改本子里这条失败")
    async def edit_note(
        entry_id: str,
        content: Annotated[
            str | None,
            Field(default=None, description="改成的新内容（不改就留空）"),
        ] = None,
        remind_at: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "改提醒时刻（ISO8601）：给没时间的补一个 = 变成日程、改一个时刻 = "
                    "改期。**想把时间撤了**（日程变回备忘）传一个空字符串 ''。不动时间就"
                    "留空（不填）。"
                ),
            ),
        ] = None,
        status: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "改状态：'done' = 做了 / 'dropped' = 不做了划掉。不改状态就留空。"
                ),
            ),
        ] = None,
    ) -> str:
        """改 / 划本子里已有的某一条（用它的 id 指到那条）。

        翻本子（read_notebook）能拿到每条的 id。可以改内容、补 / 改 / 撤提醒时间、或
        把它标成做了 / 划掉。只动你要动的，没传的字段保持原样。

        做了就标 done、不想做了就 dropped——划掉的不会再进你脑子常驻，但翻本子看全部
        时还在（不是真删，留个痕）。

        Args:
            entry_id: 要动的那条的 id（翻本子拿到）。
            content: 改成的新内容（不改留空）。
            remind_at: 改提醒时刻；空字符串 '' = 撤掉时间（变回备忘）；不填 = 不动。
            status: 'done' 做了 / 'dropped' 划掉；不填 = 不动状态。

        Returns:
            一句确认改了什么。
        """
        # remind_at 三态翻译（None 无法同时表达「没传」与「撤」）：
        #   * None（没填）         → 不动时间（clear_remind_at=False、remind_at=None）。
        #   * "" 空串（撤的信号）  → 撤时间（clear_remind_at=True）。
        #   * 非空时刻             → 设 / 改时间。
        # 空串当撤时间信号，是因为模型只能填字符串、None 是「没填」的天然默认；用显式空
        # 串承载「撤」让底层 update_entry 能区分两者（底层用独立布尔 clear_remind_at）。
        clear_remind_at = remind_at == ""
        set_remind_at = remind_at if (remind_at not in (None, "")) else None
        await update_entry(
            lane=lane,
            persona_id=persona_id,
            entry_id=entry_id,
            content=content,
            remind_at=set_remind_at,
            clear_remind_at=clear_remind_at,
            status=status,
        )
        # 待挂日程提醒（落库成功后才记）：补 / 改时间 → 给这条记一条待挂提醒（变日程 /
        # 改期）；撤时间 → 记 None（变回备忘、不挂）。只改状态 / 内容、没动时间 → 不碰
        # 容器（时间没变、原来挂的那条提醒仍由它自己的 tick 负责，gate 据 entry 当前
        # 状态判废已划掉的）。同轮多次动同一 entry_id 覆盖而非追加（最后一次为准）。
        if schedule_reminders is not None:
            if clear_remind_at:
                schedule_reminders[entry_id] = None
            elif set_remind_at is not None:
                schedule_reminders[entry_id] = set_remind_at
        changed = []
        if content is not None:
            changed.append("内容")
        if clear_remind_at:
            changed.append("撤掉了提醒时间")
        elif set_remind_at is not None:
            changed.append("提醒时间")
        if status == STATUS_DONE:
            changed.append("标成做了")
        elif status == STATUS_DROPPED:
            changed.append("划掉了")
        elif status is not None:
            changed.append("状态")
        return f"好，改了：{'、'.join(changed) if changed else '（没动什么）'}"

    @tool_error("翻本子失败")
    async def read_notebook(
        include_all: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "默认 False 只看还惦记着的（active）；True 看全部（含做过、划掉的，"
                    "找旧条目 / 睡前清理时用）。"
                ),
            ),
        ] = False,
    ) -> str:
        """完整翻一遍你的本子（找一条旧的、睡前清理时用）。

        平时还没了结的条目每轮自动进你脑子、不用主动翻。这个工具是你拿回「看全部」的
        出口：找一条记过的旧事、睡前回顾时把陈年的 / 做过的 / 不想做的清掉，都靠它把
        本子翻出来看。

        默认只列还惦记着的（你没标做了 / 没划掉的）；include_all=True 连做过、划掉的
        一起列。每条带它的 id、内容、提醒时间（有的话）、状态。

        Args:
            include_all: False 只看还活着的（默认）；True 看全部（含 done / dropped）。

        Returns:
            本子条目列表的文字呈现（每条带 id / 内容 / 时间 / 状态），空本子给一句提示。
        """
        entries = await list_notebook_entries(
            lane=lane, persona_id=persona_id, active_only=not include_all
        )
        return render_notebook(entries, now=observed_at)

    @tool_error("查这件事失败")
    async def look_up(query: str) -> str:
        """心里有件具体的事、想知道某个真答案时，带着你想好的问题去网上查一查。

        这是「带着问题去查」的手：你这一刻为某件具体的事需要知道一个信息——明天广州
        下不下雨该不该带伞、那家餐厅几点关门、某件事到底是怎么回事——就把你**自己想好
        的那个具体问题**给它，它去网上替你查回来。

        查回来的是带着出处的真东西（每条有标题、来源链接、关键摘录），不是替你嚼碎的
        一句话——你看着这些真材料自己反应，知道的就基于它说，别凭空编。要是没查到，
        它会如实告诉你没查到，那就当真没查到、别硬凑一个答案。

        想长期记住的（比如查到约会那天会下雨、决定带伞），自己记进本子；只是想知道
        一下、用过就算的，看完反应过就好，不必都记。

        这只手是「带着具体问题求答案」用的；只是没事想随便逛逛、看看有啥新鲜的，那是
        另一回事（browse_feed），不走这里。也别用 act 假装「我查了下」——act 给不了你
        真东西，那样「查到的」是你自己编的；真想知道就用这只手，它拿回来的才是真的。

        Args:
            query: 你想好的那个具体问题（自然语言，如"广州明天会下雨吗"）。

        Returns:
            带着来源的搜索结果文本（标题 / 出处 / 关键摘录）；没查到时一句如实说明。
        """
        result = await search_web.invoke({"query": query})
        # search_web 没拿到真东西（四种形态：非 str / 两条精确空文案 / "网页搜索失败: ..."
        # 前缀失败串，见 _search_web_failed_or_empty）→ 如实说没查到，绝不把失败 / 空当
        # 内容顶上去（spec 决策 4：拿不到真东西就不假装拿到了、不编一个顶上）。承重点：
        # "网页搜索失败: xxx" 这条变长失败串旧实现漏认会被当真内容包装成"查到这些"。
        if _search_web_failed_or_empty(result):
            return f"网上没查到「{query}」的结果，这事就先当查不到、别硬凑答案。"
        # 拿到带源结果：原样把 search_web 的带源文本（标题 / 出处 / 摘录）送进她当轮
        # 上下文，**不**再过一道 LLM 把它消化成一段话（承重红线：消化掉就丢了真来源、
        # 反应又变「一眼假」）。只在前面挂一句她问的问题，让上下文里清楚这批材料是为
        # 哪个问题查的。
        return f"为「{query}」查到这些（带出处，自己看真材料）：\n\n{result}"

    @tool_error("刷手机失败")
    async def browse_feed(direction: str) -> str:
        """没事想随便逛逛、看看有啥新鲜的时候，掏出手机刷一刷。

        这是「漫无目的逛一圈」的手：你这一刻没有什么非搞清楚不可的具体问题，就是想
        刷刷看——惦记的那部番更没更、喜欢的那个游戏 / 领域出新消息没、有点无聊想看点
        搞笑的。把你这会儿**想看的方向**给它就行：方向是你读自己此刻状态 / 心情后自然涌出来的、
        想往哪边逛的念头（一句大白话，可以很泛，比如"想看点搞笑的""那部番更新没"），
        不是一个标准检索词、更不用憋出精确关键词。它会拿你这个方向去刷回**一批**带着
        出处的真东西（多条，像刷出来的一屏 feed），供你一条条往下翻，翻到感兴趣的才停。

        刷你自己感兴趣圈子里的东西——你喜欢的那些、惦记的那些、当下心情想看的那些。
        时政、社会突发、灾害预警那种世界级大事不归你刷（那是世界自己会让你感知到的），
        你只管刷你自己的圈子。

        和"带着具体问题去查一个答案"是两回事：心里有件具体的事、想知道某个真答案
        （明天下不下雨、餐厅几点关），那是另一只手 look_up，不走这里。这只手是没目的
        地逛、看有啥；那只手是带着问题求答案。也别用 act 假装「我刷了刷手机」——act
        给不了你真东西，那样「刷到的」是你自己编的；真想刷就用这只手，它给你的才是真的。

        刷回来的是带着出处的真材料（每条有标题、来源链接、关键摘录），不是替你嚼碎的
        一段话——你自己一条条翻、看到感兴趣的就基于它真实反应。要是这会儿没刷到什么
        新鲜的，它会如实说没刷到，那就当真没啥可看、别硬编一批顶上。

        刷到想长期留住的（某部番、某个话题），自己记进本子；只是顺手刷过、看完就算的，
        反应过就好、不必都记。

        Args:
            direction: 你这会儿想看的方向（自然语言、可以很泛，如"想看点搞笑的"）。

        Returns:
            一批带着来源的真内容（多条，标题 / 出处 / 摘录）供你浏览；没刷到时一句如实说明。
        """
        # 拿她带进来的方向当检索方向去 search_web 捞一批（num 比 look_up 聚焦多，像一条
        # feed）。**承重红线**：方向原样作 query 传入——工具不替她改写、不写死兴趣标签
        # 规则、不另起一个 agent 替她猜该看啥（她 life 本身就是懂她的 agent，方向是她读
        # 自己状态后自然涌出的）。刷什么完全由她带的 direction 决定。
        result = await search_web.invoke(
            {"query": direction, "num": _BROWSE_FEED_BATCH}
        )
        # search_web 没拿到真东西（四种形态：非 str / 两条精确空文案 / "网页搜索失败: ..."
        # 前缀失败串，见 _search_web_failed_or_empty）→ 如实说没刷到，绝不把失败 / 空当
        # 内容编一批顶上（spec 决策 4）。承重点同 look_up："网页搜索失败: xxx" 这条变长
        # 失败串旧实现漏认会被当真内容包装成"刷到这些"。
        if _search_web_failed_or_empty(result):
            return (
                f"这会儿没刷到「{direction}」方向的新鲜内容，"
                "就当暂时没啥可看、别硬编一批。"
            )
        # 拿到一批带源结果：原样把 search_web 的整批带源文本（每条标题 / 出处 / 摘录）
        # 送进她当轮上下文供她浏览，**不**再过一道 LLM 把一批消化成一段话（承重红线：
        # 消化掉就丢了真来源、反应又变「一眼假」，也丢了「一批供她挑选」的味儿）。
        # 边界（Non-goal）只靠上面 docstring 引导，这里**绝不**对结果做关键词过滤 /
        # 黑名单拦截（过滤就是工具内替她决策、违宪）。只在前面挂一句她想看的方向。
        return f"刷「{direction}」刷到这些（带出处，自己往下翻）：\n\n{result}"

    @tool_error("安排下次醒来失败")
    async def schedule(
        seconds: Annotated[
            int,
            Field(
                description=(
                    "多少秒后再醒来过你自己的下一刻，必须在 "
                    "60～43200 之间（约 1 分钟到 12 小时）"
                )
            ),
        ],
    ) -> str:
        """排一下过多久再醒来、接着过你自己的日子。

        被起头叫醒后，没有新动静时世界不会再来敲你 —— 想接着往下过（写完这题接着写下
        一题、收拾完挪去客厅、困了睡一觉到天亮），就用它排好过多久再醒来继续。

        seconds 必须在 60～43200 之间（最短约 1 分钟、防排得太密；最长 12 小时、够你
        夜里睡一整觉到天亮）。具体睡多久你自己看现在几点定 —— 夜里可以睡久、白天睡短；
        真有要紧的动静，世界还是会立刻来敲你、把你从长睡里叫醒。超出范围会报错，请改填
        一个范围内的值重调。不排也行（那就等下一次有动静来敲你）。

        Args:
            seconds: 多少秒后再醒来继续（60 ≤ seconds ≤ 43200）。

        Returns:
            一句确认文本。
        """
        if seconds > LIFE_SCHEDULE_MAX_SECONDS:
            raise ValueError(
                f"schedule 的 seconds={seconds} 超过上限 {LIFE_SCHEDULE_MAX_SECONDS} 秒。"
                f"请改填一个 ≤ {LIFE_SCHEDULE_MAX_SECONDS} 的值重调。"
            )
        if seconds < LIFE_SCHEDULE_MIN_SECONDS:
            raise ValueError(
                f"schedule 的 seconds={seconds} 低于下限 {LIFE_SCHEDULE_MIN_SECONDS} 秒。"
                f"请改填一个 ≥ {LIFE_SCHEDULE_MIN_SECONDS} 的值重调。"
            )
        # 不直接 emit_delayed：一轮内多次 schedule / 多轮 schedule 会各排一条未来 self
        # 唤醒 → 叠加唤醒风暴。改为只把待办 self-wake 记进 round-scoped slot（覆盖而非
        # 追加 → 一轮内最后一次为准），由 engine 在循环收口后 emit 至多一条 self
        # LifeWakeTick（照搬 world sleep 的唤醒风暴命门解）。
        self_wake["delay_ms"] = seconds * 1000
        return f"好，{seconds} 秒后再醒来接着过"

    # act_tool 的函数名带 _tool 后缀避免遮蔽导入的 handler；工具对模型暴露的 name
    # 要是 "act"，所以显式覆写 Tool.name 与 definition.name。
    update_tool = Tool(update_life_state)
    act_tool_obj = Tool(act_tool)
    act_tool_obj.name = "act"
    act_tool_obj.definition.name = "act"

    # chat（说话）+ 本子三件（note / edit_note / read_notebook）+ look_up（带问题查）+
    # browse_feed（没事刷手机）都是基础工具、不依赖 self_wake，与 update / act 并列常驻。
    tools = [
        update_tool,
        act_tool_obj,
        Tool(chat),
        Tool(note),
        Tool(edit_note),
        Tool(read_notebook),
        Tool(look_up),
        Tool(browse_feed),
    ]
    # self_wake 容器给了才挂 schedule（自排工具）。旧调用方 / 工具契约测试不给，
    # 就没有 schedule（向后兼容）。
    if self_wake is not None:
        tools.append(Tool(schedule))
    return tools


async def fire_life_self_wake(
    *, lane: str, persona_id: str, self_wake: dict
) -> bool:
    """循环收口后 emit 至多一条 self ``LifeWakeTick`` + 落下次该醒时刻（唤醒风暴 + 到点 gate 命门）。

    engine 在 agent 循环跑完后调本函数（对称 world :func:`app.world.tools.fire_self_wake`）。
    ``self_wake`` 是本轮 round-scoped 待办容器（schedule 写的 ``{"delay_ms": int}``，
    覆盖而非追加 → 一轮内最后一次为准）。有待办时一次性收口三件事：

      1. 算目标唤醒时刻 = 现实 now + delay（现实 CST aware 时间，不用会因 gate 停滞的
         任何 world 时钟）。
      2. 把目标时刻写进 ``LifeState.next_wake_at``（:func:`set_life_next_wake_at`）——
         她的自排唤醒入口走 gate 时读它判到点。
      3. emit 唯一一条 self ``LifeWakeTick``，**携带这个目标时刻**（``target_wake_at``）：
         到期时与 state 当前 next_wake_at 比对判 stale（被新自排 / 外部覆盖即作废）。

    写 state 与 emit 携带同一个 target_iso（相等是 stale 判定的命门）。没调过 schedule
    （空容器）就不写、不 emit —— 她不自排接力时不会自己醒，靠 world 下一次 notify 起头。
    返回是否 emit。

    LifeWakeTick 在 life_wake engine 里 import，避免 tools ↔ engine 循环 import。
    """
    delay_ms = (self_wake or {}).get("delay_ms")
    if delay_ms is None:
        return False

    # 目标唤醒时刻 = 现实 now + delay（现实 CST aware ISO，gate 比较的口径）。
    target = cst_time.now_cst() + timedelta(milliseconds=delay_ms)
    target_iso = target.isoformat()

    # 落 next_wake_at（gate 到点判定读它）。写 state 与 emit 携带同一 target_iso：
    # 相等是 stale 判定命门 —— self 到期时只有携带的目标 == state 当前值才作数。
    await set_life_next_wake_at(
        lane=lane, persona_id=persona_id, next_wake_at=target_iso
    )

    # 延迟唤醒信号在 engine 里 import，避免 tools ↔ engine 循环 import。
    from app.nodes.life_wake import LifeWakeTick

    # emit_delayed 失败的可观测（必改 4）：life 没保底心跳，publish 失败会留一个未来 wake
    # state（next_wake_at 已写）但没实际唤醒（机械漏投 → 她不自排醒、链断）。完整恢复
    # （watchdog）是 Non-goal、后置；这里至少不静默——失败 log warning 带 lane/persona/
    # target 让 coe 能看到漏投，且不把异常往上炸：本轮的 durable 收口（标已读 / 写快照）
    # 已落地，不该被一条可选的自排漏投把整轮拖成失败重投（重投会重放 durable 工具）。
    try:
        await emit_delayed(
            LifeWakeTick(
                lane=lane,
                persona_id=persona_id,
                reason="self",
                target_wake_at=target_iso,
            ),
            delay_ms=delay_ms,
        )
    except Exception:
        logger.warning(
            "[life_tools] %s/%s fire self wake emit_delayed failed "
            "(target=%s, delay_ms=%d): self wake NOT scheduled this round "
            "(no heartbeat fallback; relies on world notify to restart)",
            lane,
            persona_id,
            target_iso,
            delay_ms,
            exc_info=True,
        )
        return False
    return True


async def fire_schedule_reminders(
    *, lane: str, persona_id: str, schedule_reminders: dict
) -> int:
    """循环收口后给本轮排 / 改的每条日程**各 emit 一条** ``ScheduleReminderTick``（日程到点提醒）。

    engine 在 agent 循环跑完后调本函数（与 :func:`fire_life_self_wake` 并列、互不干扰：
    self-wake 是「她自排下一轮节奏」走 next_wake_at 单槽覆盖；日程到点提醒是**每条日程
    各挂各的**独立一路，绝不挤进 next_wake_at）。``schedule_reminders`` 是本轮 round-scoped
    待办容器（note / edit_note 写的 ``{entry_id: remind_at | None}``，覆盖而非追加）：

      * 值为 ``remind_at`` 字符串 → emit 一条携带 ``(entry_id, remind_at)`` 的
        ``ScheduleReminderTick``，``delay_ms = max(0, remind_at - 现实 now)``。到期经
        in-process 边接回 :func:`app.nodes.life_wake.life_schedule_reminder_node`，那里读
        这条 entry 的最新一版走到点 gate（仍 active、remind_at 仍 == 携带值才作数 ——
        改期 / 划掉 / 撤时间后旧 tick 携带值对不上 → 判废）。
      * 值为 ``None``（撤了时间）→ 不挂提醒（这条日程变回备忘）。

    **过去时刻（spec edge 3）**：``remind_at`` 已早于现实 now → delay 夹到 0、立即 emit
    （下一轮就提醒，不负、不炸、不漏）。``remind_at`` 脏串解析不出 → 同样夹到 0（不静默
    把这条日程吞掉），gate 那头读 entry 当前状态对账兜底。

    **逐条失败隔离（对称 fire_life_self_wake 的可观测）**：某条 emit 失败 log warning
    （带 lane/persona/entry/target）、不往上炸、不拖垮其余条目——本轮的 durable 收口
    （记本子）已落定，不该被一条可选的到点提醒漏挂把整轮拖成失败重投（重投会重放
    durable 工具）。完整漏投恢复（watchdog）同 self-wake 是 Non-goal、后置。

    返回成功 emit 的条数。``ScheduleReminderTick`` 在 life_wake engine 里 import，避免
    tools ↔ engine 循环 import。
    """
    if not schedule_reminders:
        return 0

    from app.nodes.life_wake import ScheduleReminderTick

    now = cst_time.now_cst()
    emitted = 0
    for entry_id, remind_at in schedule_reminders.items():
        if remind_at is None:
            # 撤了时间（这条变回备忘）→ 不挂提醒。
            continue
        target = cst_time.parse(remind_at)
        if target is None:
            # 脏 remind_at 解析不出真实时刻：不静默吞这条日程，夹到立即提醒，由 gate
            # 那头读 entry 当前状态对账兜底（spec edge 3 的脏数据分支）。
            delay_ms = 0
        else:
            # delay = remind_at - 现实 now，过去时刻夹到 0（立即提醒、不负）。
            delay_ms = max(0, int((target - now).total_seconds() * 1000))
        try:
            await emit_delayed(
                ScheduleReminderTick(
                    lane=lane,
                    persona_id=persona_id,
                    entry_id=entry_id,
                    remind_at=remind_at,
                ),
                delay_ms=delay_ms,
            )
            emitted += 1
        except Exception:
            # 逐条隔离：这条挂失败不拖垮其余，log warning（不静默吞），不往上炸。
            logger.warning(
                "[life_tools] %s/%s schedule reminder emit_delayed failed "
                "(entry=%s, remind_at=%s, delay_ms=%d): this reminder NOT "
                "scheduled (others unaffected; no watchdog fallback)",
                lane,
                persona_id,
                entry_id,
                remind_at,
                delay_ms,
                exc_info=True,
            )
    return emitted
