"""Inner-context builder — what chat feeds 赤尾 each turn.

Sections, in order:
  1. World-arc awareness (only when the arc exists) — the public life stage
     this family has reached (WorldArc), rendered first-person by
     ``render_arc_awareness``. Leads the block because it changes on a
     day/week clock — stable-prefix before the per-message scene and the
     hour-level life snapshot (prompt-cache friendly). Cold chain / read
     failure → the section is simply absent, no placeholder.
  2. Scene (p2p / group / proactive) — who she's talking to, why.
  3. Relationship page (only when it exists) — her own bedtime-review page
     about the person she's talking to (trigger_user_id, p2p and group
     alike). No trigger user / no page / read failure → absent, no
     placeholder (spec decision 6).
  4. Latest day page (only when it exists) — the most recent "yesterday"
     she wrote at bedtime review, dated strictly before the current living
     day (an early-morning nap review writes a stub page for *today*; that
     stub must not pose as yesterday — same boundary as the life-side
     injection). The written_at stamp goes into the frame so she knows how
     old the memory is. No page / read failure → absent.
  5. Life snapshot — where she is, what she's doing, how she feels *right now*,
     read straight from the life engine's LifeState. This is the main subject:
     the 赤尾 talking to a real person is the 赤尾 living this moment.

The thread of the order: who you're talking to → what they are to you →
what your yesterday left you → who you are right now.

There is no RAG recall here. She speaks from what her life already knows
(the shared world stage + her own pages + current_state + mood), nothing
more — the pages are her own writing, not retrieval.
"""

from __future__ import annotations

import logging

from app.domain.arc_awareness import render_arc_awareness
from app.domain.life_state import find_life_state
from app.domain.notebook import list_notebook_entries, render_notebook
from app.infra.cst_time import now_cst
from app.life.living_day import living_day
from app.life.pages import read_day_page_before, read_relationship_page
from app.runtime.lane_policy import current_deployment_lane

logger = logging.getLogger(__name__)

# Cold start / thin state / read error all land here so inner_context never
# collapses and chat can still hold a normal conversation.
_LIFE_FALLBACK = "你此刻的状态暂时拿不到，就照你平时的样子自然聊吧。"


async def _build_life_state(persona_id: str) -> str:
    """她此刻的真实快照 (LifeState)，作为 inner_context 的主角。

    lane 口径与 world/life 写入端一致：``current_deployment_lane() or "prod"``
    （进程级泳道，prod 归一到 "prod"）。

    失败兜底（spec decision 6）：读不到快照（冷启 / 她还没活过一轮）、
    current_state 稀薄、或读取报错时，返回一句简洁兜底而非空串——
    inner_context 不能塌，chat 仍要能正常对话。
    """
    try:
        lane = current_deployment_lane() or "prod"
        snap = await find_life_state(lane=lane, persona_id=persona_id)
    except Exception as e:
        logger.warning("[%s] Failed to read life state: %s", persona_id, e)
        return _LIFE_FALLBACK

    if not snap:
        return _LIFE_FALLBACK

    current = (snap.current_state or "").strip()
    if not current:
        return _LIFE_FALLBACK

    mood = (snap.response_mood or "").strip()
    if mood:
        return f"你此刻正在：{current}\n你的心情：{mood}"
    return f"你此刻正在：{current}"


# 平直的第一人称框架标头（机制层，零剧情事实——角色名 / 日期 / 数字不进模板，
# 宪法同 arc 透传）。「这页写于 X」让她知道这份记忆的新旧。
_RELATIONSHIP_HEADER = (
    "【你们的关系】这是你睡前回顾时写下的、关于正在和你说话的这个人的一页"
)
_DAY_HEADER = "【你的昨天】这是你睡前回顾时给自己写下的最近一页"


async def _build_relationship_section(
    persona_id: str, trigger_user_id: str | None
) -> str:
    """触发人的关系页段：p2p / 群聊都按 trigger_user_id 注（spec 决策 6）。

    无触发人（proactive 可能缺位）/ 无页（第一次聊）/ narrative 空白 / 读失败
    → 返回 ""，整段缺席不补占位。读失败只 log：页注入是上下文增强，绝不能
    塌掉 chat（照 render_arc_awareness 的姿势）。
    """
    if not trigger_user_id:
        return ""
    try:
        lane = current_deployment_lane() or "prod"
        page = await read_relationship_page(
            lane=lane, persona_id=persona_id, other_user_id=trigger_user_id
        )
    except Exception as e:
        logger.warning(
            "[%s] Failed to read relationship page for %s: %s",
            persona_id,
            trigger_user_id,
            e,
        )
        return ""

    if page is None:
        return ""
    narrative = page.narrative.strip()
    if not narrative:
        return ""
    return f"{_RELATIONSHIP_HEADER}（这页写于 {page.written_at}）：\n{narrative}"


async def _build_yesterday_section(persona_id: str) -> str:
    """她最近一页昨天：日期**严格早于当前生活日**的最新一版（与 life 侧同口径）。

    跨日取最新不行：清晨回笼觉的快班会给**当前生活日**写下凌晨短页，下午聊天
    会把它错当「你的昨天」注进来（2026-06-12 真实群聊 trace 实证）。上界按
    生活日（04:00 晨界）算——熬夜聊到凌晨两点，「昨天」仍是前一个生活日之前。

    无页（冷启动：她还没有昨天可忆）/ narrative 空白 / 读失败 → 返回 ""，
    整段缺席不补占位，失败兜底同 _build_relationship_section。
    """
    try:
        lane = current_deployment_lane() or "prod"
        page = await read_day_page_before(
            lane=lane,
            persona_id=persona_id,
            before_date=living_day(now_cst()),
        )
    except Exception as e:
        logger.warning("[%s] Failed to read latest day page: %s", persona_id, e)
        return ""

    if page is None:
        return ""
    narrative = page.narrative.strip()
    if not narrative:
        return ""
    return f"{_DAY_HEADER}（这页写于 {page.written_at}）：\n{narrative}"


# 平直的第一人称框架标头（机制层，零剧情事实，宪法同其它段）。
_NOTEBOOK_HEADER = "【你本子里还没了结的事】"


async def _build_notebook_section(persona_id: str) -> str:
    """她本子里还没了结的事段（备忘录 & 日程 第二块 · chat 侧）.

    chat 概念上是 life 的快照，但工程上 inner_context 是显式拼几段——本子得**显式接
    进去**才会出现在聊天里。读她**还活着**的条目（active_only=True：她自己没标 done /
    dropped 的），原样渲染（复用 render_notebook，与 read_notebook 工具 / life 唤醒同
    一份）。**只读、不改状态、不删**；**绝不**按年龄 / 条数 / 过期筛——那是代码替她决
    定忘掉什么、违宪。lane 口径与 _build_life_state 一致（进程级泳道，prod 归一 "prod"）。
    now 用现实此刻（派生「到点了」标签）。

    空本子 / 读失败 → 返回 ""，整段缺席不补占位。读失败只 log：本子注入是上下文增强，
    绝不能塌掉 chat（照 _build_yesterday_section 的姿势）。
    """
    try:
        lane = current_deployment_lane() or "prod"
        entries = await list_notebook_entries(
            lane=lane, persona_id=persona_id, active_only=True
        )
    except Exception as e:
        logger.warning("[%s] Failed to read notebook: %s", persona_id, e)
        return ""

    if not entries:
        return ""
    body = render_notebook(entries, now=now_cst().isoformat())
    return f"{_NOTEBOOK_HEADER}（你自己记下、还没标做了 / 划掉的）：\n{body}"


def _scene_section(
    chat_type: str,
    chat_name: str,
    trigger_username: str | None,
    is_proactive: bool,
    proactive_stimulus: str,
) -> str:
    if is_proactive:
        scene = f"你在群聊「{chat_name}」中。" if chat_name else ""
        scene += "\n你刚刷到了群里的对话。如果你想说点什么就说，不想说也可以不说。"
        scene += "\n不要刻意解释为什么突然说话，像朋友在群里自然接话就好。"
        if proactive_stimulus:
            scene += f"\n（你注意到的：{proactive_stimulus}）"
        return scene
    if chat_type == "p2p":
        return f"你正在和 {trigger_username} 私聊。" if trigger_username else ""
    parts = []
    if chat_name:
        parts.append(f"你在群聊「{chat_name}」中。")
    if trigger_username:
        parts.append(f"需要回复 {trigger_username} 的消息（消息中用 ⭐ 标记）。")
    return "\n".join(parts)


async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str | None,
    trigger_username: str | None,
    persona_id: str,
    chat_name: str = "",
    *,
    is_proactive: bool = False,
    proactive_stimulus: str = "",
) -> str:
    """Assemble inner_context: arc (when present) + scene + her pages (when
    present) + life snapshot."""

    sections: list[str] = []

    # 世界阶段透传：对话里她也必须知道自己人生走到哪页（世界阶段翻页后 persona
    # 出厂设定可能已过时）。lane 口径与 _build_life_state 一致（进程级泳道，prod
    # 归一 "prod"）；render 空链 / 读失败返回 "" → 整段缺席、不塞占位。
    arc_awareness = await render_arc_awareness(
        lane=current_deployment_lane() or "prod"
    )
    if arc_awareness:
        sections.append(arc_awareness)

    scene = _scene_section(
        chat_type, chat_name, trigger_username, is_proactive, proactive_stimulus
    )
    if scene:
        sections.append(scene)

    # 睡前回顾两页：场景之后、人生快照之前——你在和谁聊 → 你们的关系 →
    # 你的昨天 → 你此刻状态。无页 / 无触发人 / 读失败 → 整段缺席不补占位。
    relationship = await _build_relationship_section(persona_id, trigger_user_id)
    if relationship:
        sections.append(relationship)

    yesterday = await _build_yesterday_section(persona_id)
    if yesterday:
        sections.append(yesterday)

    # 她本子里还没了结的事：显式接进 chat（工程上 inner_context 是显式拼的，本子不接
    # 就不会出现在聊天里）。位置在昨天页之后、人生快照之前——你的昨天 → 你还惦记着的
    # 事 → 你此刻状态。无条目 / 读失败 → 整段缺席不补占位。
    notebook = await _build_notebook_section(persona_id)
    if notebook:
        sections.append(notebook)

    sections.append(await _build_life_state(persona_id))

    return "\n\n".join(sections)
