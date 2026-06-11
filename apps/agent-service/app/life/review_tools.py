"""睡前回顾的写入工具 — 昨天页 + 关系页两件（``LIFE_REVIEW_TOOLS``）.

回顾本体跑一个无会话 Agent（不背当天意识流的叙事惯性、从证据现判），以她本人
第一人称回看刚结束的生活日，手里只有这两件工具：

  * :func:`update_day_page`          —— 整篇重写「这一天留下来的几笔」（昨天页）。
  * :func:`update_relationship_page` —— 为一个当天聊过的真人整篇重写「他与我」
    （关系页）。

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

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.infra import cst_time
from app.life.pages import write_day_page, write_relationship_page

# 回顾本体往 AgentContext.features 塞的三个机制绑定 key（不散落字符串、从这里
# import）。命名带 life_review 前缀，与 world 的 world_lane / world_round_id
# 同风格、不同命名空间——两边的 ambient 绑定绝不互相误读。
FEATURE_REVIEW_LANE = "life_review_lane"
FEATURE_REVIEW_PERSONA = "life_review_persona_id"
FEATURE_REVIEW_TARGET_DATE = "life_review_target_date"


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


# 回顾工具集（两件）：昨天页 + 关系页。与 life 活轮的工具（update_life_state /
# act / chat / schedule）物理隔离——回顾是睡前的另一双手，不碰活轮的手。
LIFE_REVIEW_TOOLS = [update_day_page, update_relationship_page]
