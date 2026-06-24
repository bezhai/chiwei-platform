"""Inner-context builder — what chat feeds 赤尾 each turn.

Sections, in order:
  1. World-arc awareness (only when the arc exists) — the public life stage
     this family has reached (WorldArc), rendered first-person by
     ``render_arc_awareness``. Leads the block because it changes on a
     day/week clock — stable-prefix before the per-message scene and the
     hour-level life snapshot (prompt-cache friendly). Cold chain / read
     failure → the section is simply absent, no placeholder.
  2. Scene — two orthogonal dimensions, each labeled (spec decision 7):
     communication medium (Feishu p2p / group — she is typing over Feishu,
     never face-to-face) and physical presence (her real here-and-now scene
     is carried by the life snapshot below; the chat peer is on the other
     end of Feishu, not beside her). Never collapsed into one "current
     occasion" label — collapsing them is the root of "treating Feishu as
     face-to-face".
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
from app.domain.book_impression import (
    find_current_book_impression,
    render_reading_impression,
)
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


async def _build_reading_impression_section(persona_id: str) -> str:
    """她正在读的那本书的印象段（读小说 Task 3 · chat 侧）.

    chat 概念上是 life 的快照，但工程上 inner_context 是显式拼几段——在读的书得**显式
    接进去**才会出现在聊天里。读她**当前在读那一本**（find_current_book_impression 取她
    最近读过一程、状态仍「在读」那个附件实例——读完 / 放下的已排除，只渲一本当前书），
    渲染复用 render_reading_impression（单一定义处，与 life 唤醒侧同一份）。书名由印象
    自带（book_title），**不再 find_book_meta**（书注册表已删，Task 3）。每轮从 PG 重读
    重渲，所以聊天时书自然在她心里。lane 口径与 _build_life_state 一致（进程级泳道，prod
    归一 "prod"）。信息差不破：只读她自己的 BookImpression（她的私人印象），绝不碰 world
    全局快照。

    无当前书（None：读完 / 放下 / 没开读）/ 读失败 → 返回 ""，整段缺席不补占位。读失败
    只 log：在读印象注入是上下文增强，绝不能塌掉 chat（照 _build_notebook_section 的姿势）。
    """
    try:
        lane = current_deployment_lane() or "prod"
        impression = await find_current_book_impression(
            lane=lane, persona_id=persona_id
        )
    except Exception as e:
        logger.warning(
            "[%s] Failed to read current book impression: %s", persona_id, e
        )
        return ""

    if impression is None:
        return ""
    return render_reading_impression(impression)


# 两个正交维度的段标头（spec 决策 7：物理在场 / 通信介质各自标清，绝不压成一个
# 「当前场合」标签——混成一个字段正是「把飞书当当面」的根）。机制层框架文案、零剧情
# 事实（同 arc / 页透传的宪法）。
_MEDIUM_HEADER = "【通信介质】"
_PRESENCE_HEADER = "【物理在场】"

# 物理在场维度的固定提示：聊天对象隔着飞书在另一端、**不在她身边**，她此刻真正所处
# 的物理场景由下面的人生快照（LifeState）承载。这条治「把聊天对象当成在身边」的混淆。
_PRESENCE_HINT = (
    f"{_PRESENCE_HEADER}你此刻真正所处的场景、身边有谁，看下面你自己的此刻状态。"
    "正在和你打字的人隔着飞书在另一端，不在你身边。"
)


def _scene_section(
    chat_type: str,
    chat_name: str,
    trigger_username: str | None,
) -> str:
    """把这次交流的两个正交维度分别标清（spec 决策 7）：通信介质 + 物理在场。

    **通信介质**：她正通过什么跟谁打字——飞书私聊 / 飞书群聊。chat 触发永远是隔着
    飞书打字、**不是当面**，这条标清治「把飞书群聊 / 私聊当成当面」的混淆。
    **物理在场**：她此刻真正所处的物理场景由人生快照（下面的 LifeState）承载，聊天
    对象在飞书另一端、不在她身边——两维度不压成一个「当前场合」标签。
    """
    medium_parts: list[str] = []
    if chat_type == "p2p":
        if trigger_username:
            medium_parts.append(
                f"{_MEDIUM_HEADER}你正通过飞书私聊和 {trigger_username} 打字"
                "（隔着飞书，不是当面）。"
            )
    else:
        if chat_name:
            medium_parts.append(
                f"{_MEDIUM_HEADER}你正在飞书群聊「{chat_name}」里打字"
                "（隔着飞书，不是当面）。"
            )
        if trigger_username:
            medium_parts.append(
                f"需要回复 {trigger_username} 的消息（消息中用 ⭐ 标记）。"
            )

    # 没有可标的通信介质（无对方名 / 无群名）时整段缺席——不硬塞物理在场提示
    # （它依附于「有一次交流」这个前提）。
    if not medium_parts:
        return ""

    return "\n".join(medium_parts) + "\n" + _PRESENCE_HINT


async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str | None,
    trigger_username: str | None,
    persona_id: str,
    chat_name: str = "",
) -> str:
    """Assemble inner_context: arc (when present) + scene + her pages (when
    present) + life snapshot.

    The scene now spells out two orthogonal dimensions (spec decision 7):
    communication medium (Feishu p2p / group — never face-to-face) and
    physical presence (carried by the life snapshot below), so chat stops
    conflating "typing over Feishu" with "being in the same room"."""

    sections: list[str] = []

    # 世界阶段透传：对话里她也必须知道自己人生走到哪页（世界阶段翻页后 persona
    # 出厂设定可能已过时）。lane 口径与 _build_life_state 一致（进程级泳道，prod
    # 归一 "prod"）；render 空链 / 读失败返回 "" → 整段缺席、不塞占位。
    arc_awareness = await render_arc_awareness(
        lane=current_deployment_lane() or "prod"
    )
    if arc_awareness:
        sections.append(arc_awareness)

    # 场景段标清两个正交维度（通信介质 + 物理在场，spec 决策 7）：物理在场指向下面的
    # 人生快照（她此刻真正在哪），通信介质标明这次是隔着飞书私聊 / 群聊打字、不是当面。
    scene = _scene_section(chat_type, chat_name, trigger_username)
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

    # 她正在读的那本书的印象：显式接进 chat（工程上 inner_context 是显式拼的，在读的书
    # 不接就不会出现在聊天里）。位置在本子段之后、人生快照之前——你还惦记着的事 → 你正
    # 在读的书 → 你此刻状态。只渲一本当前书（读完/放下的已排除）。无当前书 / 读失败 →
    # 整段缺席不补占位。
    reading = await _build_reading_impression_section(persona_id)
    if reading:
        sections.append(reading)

    sections.append(await _build_life_state(persona_id))

    return "\n\n".join(sections)
