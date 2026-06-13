"""睡前回顾的写入工具 — 昨天页 + 关系页 + 翻清本子三件（``LIFE_REVIEW_TOOLS``）.

回顾本体跑一个无会话 Agent（不背当天意识流的叙事惯性、从证据现判），以她本人
第一人称回看刚结束的生活日，手里只有这三件工具：

  * :func:`update_day_page`          —— 整篇重写「这一天留下来的几笔」（昨天页）。
  * :func:`update_relationship_page` —— 为一个当天聊过的真人整篇重写「他与我」
    （关系页）。
  * :func:`tidy_notebook_entry`      —— 睡前清本子：把做过的标 done、过时 / 不想
    做的标 dropped、还惦记的改时间（Block 4）。底层落到与活轮 ``edit_note`` 共用
    的 :func:`app.domain.notebook.update_entry`，回顾侧不重写清理逻辑——只是给她
    睡前另一双手能动本子。清什么、留什么全是她在回顾里自己判断，没有任何按年龄 /
    过期 / 条数的确定性清理规则（spec「日程过了咋办」第 2 层、赤尾宪法）。

契约照 world 反思工具（update_arc / update_attention，WorldArc 范式的工具面
第四、五次复用）：

  * **签名只留语义参数**（narrative / other_user_id）——lane / persona / 目标
    生活日是机制层的事，从 ambient :class:`~app.agent.context.AgentContext` 的
    ``features`` 读（key 见下方常量），不放进签名让模型填。
  * **时间自填**：``written_at`` 由工具体填现实当前 CST（客观时间不让模型编）。
  * **故意不包 @tool_error**：两件都是回顾环节的 durable 写，写库失败若被包成
    tool result 字符串喂回模型，Agent.run 会正常返回 → 回顾误判成功 → 假成功落
    当日 marker → 下一班（凌晨对账）重试被吃掉。让异常照实穿透炸掉整次回顾
    （``Tool.invoke`` 的设计语义：未包 @tool_error 即传播），由回顾的 fail-open
    接住：不落 marker、下一班重跑。durable mutation 失败要可见。
  * **工具集物理隔离**：``LIFE_REVIEW_TOOLS`` 只有这两件，与 life 活轮的工具
    （update_life_state / act / chat / schedule，per-round 闭包造）互不相通——
    回顾无手碰活轮、活轮无手碰页，靠隔离不靠嘱咐。

ambient features key 约定（回顾本体构造 AgentContext 时三个都要塞齐）：
``FEATURE_REVIEW_LANE`` / ``FEATURE_REVIEW_PERSONA`` / ``FEATURE_REVIEW_TARGET_DATE``。
target_date 是「生活日」标签（[04:00, 次日 04:00) 的日期），由回顾本体按钟的
约定算好塞入——日期不进工具签名：23:30 入睡写的是当日、熬夜 01:30 入睡写的是
前一日，这个判断是钟的事，不是模型的事。缺绑定时 :func:`_require_feature` 抛
LookupError 失败快——空 lane / 空 persona 落库会写出永远读不回来的脏 Key，
比炸掉整次回顾更糟。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.domain.notebook import update_entry
from app.infra import cst_time
from app.life.pages import write_day_page, write_relationship_page

# 回顾本体往 AgentContext.features 塞的三个机制绑定 key（不散落字符串、从这里
# import）。命名带 life_review 前缀，与 world 的 world_lane / world_round_id
# 同风格、不同命名空间——两边的 ambient 绑定绝不互相误读。
FEATURE_REVIEW_LANE = "life_review_lane"
FEATURE_REVIEW_PERSONA = "life_review_persona_id"
FEATURE_REVIEW_TARGET_DATE = "life_review_target_date"
# round-scoped 待挂日程提醒容器（``{entry_id: remind_at | None}``）的 features key
# （备忘录 & 日程 bug 1：回顾里改期也要挂新 tick）。回顾本体每轮往 features 塞一个
# 空 dict，tidy_notebook_entry 改 / 设成未来提醒时刻时往里记 entry_id → remind_at
# （撤时间记 None、了结 done/dropped 不碰），回顾本体收口复用
# :func:`app.nodes.life_tools.fire_schedule_reminders` 给每条各挂一条 tick——与活轮
# edit_note 写 round-scoped 容器、engine 收口同款（挂 tick 逻辑单一来源、不另写一套）。
FEATURE_REVIEW_SCHEDULE_REMINDERS = "life_review_schedule_reminders"


def _require_feature(key: str) -> str:
    """从 ambient context features 读一个机制绑定，缺了就失败快。

    没绑 context 时 ``get_context()`` 本身抛 LookupError；绑了但 features 没塞
    齐（回顾本体的 wiring bug）也抛 LookupError——绝不拿空字符串当 Key 落库。
    """
    value = get_context().features.get(key, "")
    if not value:
        raise LookupError(
            f"ambient context features 缺少 {key!r}——回顾本体构造 AgentContext "
            "时必须塞齐 lane / persona / target_date 三个绑定"
        )
    return value


@tool
async def update_day_page(narrative: str) -> str:
    """写下这一天在你心里留下来的几笔（你的昨天页）。

    回看这一整天的经历，把真正留下来的东西写成一页：触动你的事、心里起伏的
    瞬间、悬着没落地的念头。写「留下来的几笔」，**不写流水账**——不是把一天
    按时间顺序复述一遍，而是这一天过完、心里还剩下什么。只写真实经历过的，
    不编没发生的事。

    每次调用都是**整篇重写这个生活日的页**：再写一次就是新的一版**取代**旧版
    （晚上写过、凌晨再回看补写是常态），不是往后追加。

    写给哪一天、写下的时刻由系统自动记（你不用、也不能填日期和时间）。

    Args:
        narrative: 这一天留下来的几笔（整篇重写的自然语言全文）。

    Returns:
        一句确认文本。
    """
    lane = _require_feature(FEATURE_REVIEW_LANE)
    persona_id = _require_feature(FEATURE_REVIEW_PERSONA)
    # 目标生活日从 ambient 绑定读（钟的约定：23:30 入睡是当日、熬夜 01:30 是
    # 前一日——这个判断归回顾本体的钟，不进工具签名让模型填）。
    date = _require_feature(FEATURE_REVIEW_TARGET_DATE)
    # written_at 跟现实走，由代码填现实当前 CST（客观时间不让模型编，对称
    # update_arc 的 turned_at）。
    written_at = cst_time.now_cst_iso()
    await write_day_page(
        lane=lane,
        persona_id=persona_id,
        date=date,
        narrative=narrative,
        written_at=written_at,
    )
    return "已写下这一天的页"


@tool
async def update_relationship_page(other_user_id: str, narrative: str) -> str:
    """为今天聊过的一个人，整篇重写你心里「他与我」的那一页。

    写**他与你的关系**，不是他的档案：他是个什么样的人、你们之间是什么温度、
    一起经历过什么沉下来的事、有什么没聊完的线头、你跟他怎么相处。自然语言
    写成一段，不拆条目。只写真实聊过、经历过的，不编。

    每次调用都是**整篇重写这一页**：拿旧的一页和今天的相处现判，新版**取代**
    旧版，不是往后追加。关系淡了没有"删除"一说——就在重写里自然淡出，让位给
    更近的事。页有篇幅感：**一页之内**，旧的让位新的，别越写越长。

    写下的时刻由系统自动记（你不用、也不能填时间）。

    Args:
        other_user_id: 这一页写谁（对方的用户标识，从当天聊天证据里给出的那个）。
        narrative: 「他与我」的整页全文（整篇重写的自然语言）。

    Returns:
        一句确认文本。
    """
    lane = _require_feature(FEATURE_REVIEW_LANE)
    persona_id = _require_feature(FEATURE_REVIEW_PERSONA)
    # written_at 跟现实走，由代码填现实当前 CST（客观时间不让模型编）。
    written_at = cst_time.now_cst_iso()
    await write_relationship_page(
        lane=lane,
        persona_id=persona_id,
        other_user_id=other_user_id,
        narrative=narrative,
        written_at=written_at,
    )
    return f"已重写你心里 {other_user_id} 的那一页"


@tool
async def tidy_notebook_entry(
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
    """睡前清本子：动你本子里已有的某一条（用它的 id 指到那条）。

    回顾证据里【你本子的全貌】那节摆着你本子里的全部条目——还惦记的、做过的、过时
    没处理的都在，每条带它的 id。睡前把本子收拾一下：

      * 真做过了的 → status='done' 标做了；
      * 过时了 / 现在不想做了的 → status='dropped' 划掉；
      * 还惦记、只是没赶上的 → 改个时间（remind_at）重新排，或就留着不动。

    清什么、留什么是你自己的判断——没人替你按"多久没动""过期了"去删。做了 / 划掉的
    不会再每轮进你脑子常驻，但翻本子看全部时还在（不是真删，留个痕）。只动你要动的，
    没传的字段保持原样。

    **顺手做的事，记得在 update_day_page 里写进今天的页**——今天真做成了的、了结了
    的，是这一天留下来的几笔的一部分。

    Args:
        entry_id: 要动的那条的 id（证据里本子全貌那节给出）。
        content: 改成的新内容（不改留空）。
        remind_at: 改提醒时刻；空字符串 '' = 撤掉时间（变回备忘）；不填 = 不动。
        status: 'done' 做了 / 'dropped' 划掉；不填 = 不动状态。

    Returns:
        一句确认改了什么。
    """
    lane = _require_feature(FEATURE_REVIEW_LANE)
    persona_id = _require_feature(FEATURE_REVIEW_PERSONA)
    # remind_at 三态翻译（与活轮 edit_note 同口径，单一约定）：None=没传别动、
    # 空串=撤时间（clear_remind_at=True）、非空时刻=设 / 改。底层用独立布尔区分
    # 「没传」与「撤」。不在回顾侧另造一套时间语义——本子的撤 / 改时间只有一份约定。
    clear_remind_at = remind_at == ""
    set_remind_at = remind_at if (remind_at not in (None, "")) else None
    # 不包 @tool_error（同两件页工具）：durable 清理写失败若被吞成 tool result，
    # 回顾会误判成功落 marker、下一班重试被吃掉。让异常穿透炸掉整次回顾，由 fail-open
    # 接住（不落 marker、下一班重跑）。划掉 / 做掉后那条原来挂的日程到点提醒由
    # life_wake 的到点 gate 据 entry 当前状态判废（status 不在 ACTIVE_STATUSES →
    # 不再误触发），回顾侧无需另外作废提醒。
    await update_entry(
        lane=lane,
        persona_id=persona_id,
        entry_id=entry_id,
        content=content,
        remind_at=set_remind_at,
        clear_remind_at=clear_remind_at,
        status=status,
    )
    # 待挂日程提醒（bug 1：回顾里改期也要挂新 tick，与活轮 edit_note 同款）：补 / 改成
    # 未来提醒时刻 → 给这条记一条待挂提醒（变日程 / 改期），回顾本体收口
    # fire_schedule_reminders 给它挂新 tick——否则旧 tick 被 stale gate 判废、新时刻没有
    # 新 tick、这条日程静默再不提醒。撤时间 → 记 None（变回备忘、不挂）。**标 done /
    # dropped（了结）或只改内容、没动时间 → 不碰容器**：了结不需要挂 tick（划掉 / 做掉
    # 的日程旧 tick 由到点 gate 据状态判废）。容器从 ambient features 读、没塞（向后兼容）
    # 就跳过——挂 tick 逻辑只此一份 + life_tools 那份的同款，不另写第三套。
    reminders = get_context().features.get(FEATURE_REVIEW_SCHEDULE_REMINDERS)
    if reminders is not None:
        if clear_remind_at:
            reminders[entry_id] = None
        elif set_remind_at is not None:
            reminders[entry_id] = set_remind_at
    changed = []
    if content is not None:
        changed.append("内容")
    if clear_remind_at:
        changed.append("撤掉了提醒时间")
    elif set_remind_at is not None:
        changed.append("提醒时间")
    if status == "done":
        changed.append("标成做了")
    elif status == "dropped":
        changed.append("划掉了")
    elif status is not None:
        changed.append("状态")
    return f"好，收拾了本子这条：{'、'.join(changed) if changed else '（没动什么）'}"


# 回顾工具集（三件）：昨天页 + 关系页 + 翻清本子。与 life 活轮的工具
# （update_life_state / act / chat / schedule / note / edit_note / read_notebook）
# 物理隔离——回顾是睡前的另一双手，不碰活轮的手。清本子的 tidy_notebook_entry 与
# 活轮 edit_note 是不同工具集里的两双手，底层共用 update_entry。
LIFE_REVIEW_TOOLS = [update_day_page, update_relationship_page, tidy_notebook_entry]
