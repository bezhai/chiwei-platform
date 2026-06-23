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

import html
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


# 两个正交维度的段标头（spec 决策 7：物理在场 / 通信介质各自标清，绝不压成一个
# 「当前场合」标签——混成一个字段正是「把飞书当当面」的根）。机制层框架文案、零剧情
# 事实（同 arc / 页透传的宪法）。
_MEDIUM_HEADER = "【通信介质】"
_PRESENCE_HEADER = "【物理在场】"

# channel → 平台展示名（spec 决策 3：环境标识一处治理）。场景描述里的平台名由真实
# channel 决定、不再写死「飞书」——四个对话场景共用这一处映射，接 QQ 时换 channel 即
# 正确。漏配 / 未知 channel 走中性降级（_PLATFORM_NEUTRAL），**绝不**默认回飞书
# （否则接新渠道照样穿帮）。
_CHANNEL_PLATFORM_NAMES: dict[str, str] = {
    "lark": "飞书",
    "qq": "QQ",
}
_PLATFORM_NEUTRAL = "网络"


def _platform_name(channel: str | None) -> str:
    """channel → 平台展示名，未知 / 漏配中性降级（绝不回飞书）。

    spec 决策 3：平台名参数化、prompt 里不再写死飞书。``lark`` → 飞书，其余已登记
    channel → 对应平台名；登记缺失 / channel 为空 → 中性「网络」，让接新渠道时哪怕
    忘配展示名也只是退化成中性措辞、不会冒出错误的「飞书」当场穿帮。
    """
    if not channel:
        return _PLATFORM_NEUTRAL
    return _CHANNEL_PLATFORM_NAMES.get(channel, _PLATFORM_NEUTRAL)

# 物理在场维度的固定提示：聊天对象隔着网络在另一端、**不在她身边**，她此刻真正所处
# 的物理场景由下面的人生快照（LifeState）承载。这条治「把聊天对象当成在身边」的混淆。
# 平台名由 channel 决定（spec 决策 3），所以提示文案按平台名拼出来、不写死飞书。
def _presence_hint(platform: str) -> str:
    return (
        f"{_PRESENCE_HEADER}你此刻真正所处的场景、身边有谁，看下面你自己的此刻状态。"
        f"正在和你打字的人隔着{platform}在另一端，不在你身边。"
    )


def _scene_section(
    chat_type: str,
    chat_name: str,
    trigger_username: str | None,
    channel: str | None = None,
) -> str:
    """把这次交流的两个正交维度分别标清（spec 决策 7）：通信介质 + 物理在场。

    **通信介质**：她正通过什么跟谁打字——平台私聊 / 平台群聊。平台名由真实 ``channel``
    决定（spec 决策 3，一处治理、四场景共用），lark → 飞书、其余按登记取、漏配中性降级，
    绝不写死飞书。chat 触发永远是隔着网络打字、**不是当面**，这条标清治「把群聊 / 私聊
    当成当面」的混淆。
    **物理在场**：她此刻真正所处的物理场景由人生快照（下面的 LifeState）承载，聊天
    对象在网络另一端、不在她身边——两维度不压成一个「当前场合」标签。

    私聊 scene **不盖任何系统身份后缀**（修复 1）：纯文本身份后缀必被伪造——冒充者把
    昵称改成「老原（你的主人）」时，输出跟真主人的「原智鸿（你的主人）」文本形态一样、
    LLM 分不出。所以 scene 句子只把对方名字当称呼用，对方是不是主人完全交给 history 里
    他那条消息的结构化 ``<msg rel=owner>``（与群聊 scene 一致——群聊 scene 也不标身份）。
    用户可控的 ``trigger_username`` / ``chat_name`` 都经 ``html.escape`` 转义（特殊字符
    突不破结构、伪造不出标注）。
    """
    platform = _platform_name(channel)
    chat_name = html.escape(chat_name or "")
    username = html.escape(trigger_username or "")
    medium_parts: list[str] = []
    if chat_type == "p2p":
        if username:
            medium_parts.append(
                f"{_MEDIUM_HEADER}你正通过{platform}私聊和 {username}"
                f" 打字（隔着{platform}，不是当面）。"
            )
    else:
        if chat_name:
            medium_parts.append(
                f"{_MEDIUM_HEADER}你正在{platform}群聊「{chat_name}」里打字"
                f"（隔着{platform}，不是当面）。"
            )
        if username:
            medium_parts.append(
                f"需要回复 {username} 的消息。"
            )

    # 没有可标的通信介质（无对方名 / 无群名）时整段缺席——不硬塞物理在场提示
    # （它依附于「有一次交流」这个前提）。
    if not medium_parts:
        return ""

    return "\n".join(medium_parts) + "\n" + _presence_hint(platform)


async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str | None,
    trigger_username: str | None,
    persona_id: str,
    chat_name: str = "",
    channel: str | None = None,
) -> str:
    """Assemble inner_context: arc (when present) + scene + her pages (when
    present) + life snapshot.

    The scene now spells out two orthogonal dimensions (spec decision 7):
    communication medium (platform p2p / group — never face-to-face) and
    physical presence (carried by the life snapshot below), so chat stops
    conflating "typing over the network" with "being in the same room". The
    platform name in the medium dimension follows the real ``channel`` (spec
    decision 3): lark → 飞书, others by registry, missing/unknown → neutral —
    never hard-wired to 飞书."""

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
    # 人生快照（她此刻真正在哪），通信介质标明这次是隔着平台私聊 / 群聊打字、不是当面。
    # 平台名由真实 channel 决定（spec 决策 3），不再写死飞书；scene **不盖任何系统身份
    # 后缀**（修复 1：纯文本后缀必被伪造），对方是不是主人交给 history 的结构化 rel。
    scene = _scene_section(
        chat_type, chat_name, trigger_username, channel=channel
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
