"""LLM message list builders for chat (private to chat/).

Extracted from chat/context.py per Phase 6 v4 §3.1: keep the human-chat context
builder slim by moving message-shape construction into a focused module.

Task 3: history每条消息渲染成结构化标签（类 XML）。发言人身份按
``common_user_id`` 盖章——``rel`` 属性只来自 ``get_relation``（owner / None），
绝不取决于可改的显示名；所有用户来源字串（显示名、正文）进结构化
文本都经 ``html.escape`` 转义，绝不僭越成控制属性。三种伪造（改名 / 正文自称 /
闭合标签）由此全部失效。
"""
from __future__ import annotations

import html
import logging
from collections.abc import Callable

from app.agent.neutral import ContentBlock, Message, Role
from app.agent.prompts import get_prompt
from app.chat.content_parser import parse_content
from app.chat.quick_search import QuickSearchResult
from app.memory.identity_registry import get_relation

logger = logging.getLogger(__name__)


def _esc(s: str | None) -> str:
    """转义所有用户来源字串（显示名 / 正文 / 群名等）进结构化文本。

    ``html.escape(quote=True)`` 把 ``& < > " '`` 全转义——正文写 ``</msg>`` 突不破
    结构、昵称塞引号伪造不出控制属性。None → 空串（不渲染成字面 "None"）。
    """
    return html.escape(s or "", quote=True)


def format_message_tag(
    *,
    speaker: str | None,
    rel: str | None,
    time_str: str,
    body: str,
    marker: str = "",
) -> str:
    """把一条消息渲染成结构化标签：``<msg from=.. rel=.. time=..>正文</msg>``。

    ``rel`` 是系统按 common_user_id 算出的关系标签（owner / None），是唯一身份权威——
    None 时整个 ``rel`` 属性缺席（spec fail-closed：拿不到 / 未登记 → 无身份，绝不回退
    显示名当身份）。``from`` 装显示名、标签体装正文，两者都已转义（控制属性只装系统按
    id 算出的值，用户字串只待在被转义的属性值 / 标签体里、突不破结构）。``marker``
    （如群聊触发的 ⭐）作为标识属性透传。
    """
    attrs = [f'from="{_esc(speaker)}"']
    if rel:
        attrs.append(f'rel="{_esc(rel)}"')
    if marker:
        attrs.append(f'marker="{_esc(marker)}"')
    if time_str:
        attrs.append(f'time="{_esc(time_str)}"')
    return f"<msg {' '.join(attrs)}>{_esc(body)}</msg>"


def image_fn(image_key_to_filename: dict[str, str]) -> Callable[[int, str], str]:
    """Return an image render function for parse_content.render()."""

    def _fn(_i: int, key: str) -> str:
        fn = image_key_to_filename.get(key)
        return f"@{fn}" if fn else "[图片]"

    return _fn


def extract_reply_chain(
    messages: list[QuickSearchResult], trigger_id: str
) -> tuple[list[QuickSearchResult], list[QuickSearchResult]]:
    """Trace reply_message_id chain from trigger upward.

    Returns (chain_messages, other_messages), both in time-ascending order.
    """
    msg_map = {msg.message_id: msg for msg in messages}
    chain_ids: set[str] = set()
    current_id: str | None = trigger_id

    while current_id and current_id in msg_map:
        chain_ids.add(current_id)
        current_id = msg_map[current_id].reply_message_id

    chain = [m for m in messages if m.message_id in chain_ids]
    other = [m for m in messages if m.message_id not in chain_ids]
    return chain, other


def _speaker_of(msg: QuickSearchResult) -> str:
    """群上下文里这条消息的说话人显示名（仅显示名，身份另由 rel 盖章）。

    身份全局化删了 lark_user JOIN：assistant 行本就无 username，
    历史 user 行迁移前也全空。assistant 行按 role 派生固定说话人（用 "我"，不读
    username），只有 user 行才 `username or 占位`，避免把赤尾历史发言渲染成占位词。
    注意：这里只决定显示名，「是不是主人 / 姐妹」由 rel 承载、绝不取决于这个名字。
    """
    if msg.role == "assistant":
        return "我"
    return msg.username or "未知用户"


async def _rel_of(msg: QuickSearchResult) -> str | None:
    """这条消息发言人的可信关系标签（owner / None）。

    spec 决策 1：rel **只来自** ``get_relation``（按 common_user_id =
    ``msg.user_id``），是 prompt 里唯一身份权威。assistant 行是赤尾自己、不盖 rel。
    fail-closed：拿不到 common_user_id（空 user_id）→ None，绝不据空 id 查 / 绝不
    回退显示名当身份。
    """
    if msg.role == "assistant":
        return None
    if not msg.user_id:
        return None
    return await get_relation(msg.user_id)


async def build_group_messages(
    messages: list[QuickSearchResult],
    trigger_id: str,
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
) -> list[Message]:
    """Build group chat message list.

    Reply chain messages include image content blocks; other messages
    reference images as @N.png in text only.

    Task 3: 每条历史渲染成结构化标签，发言人 rel 属性按 common_user_id 盖章
    （命中主人 → owner），用户字串全转义。
    """
    chain, other = extract_reply_chain(messages, trigger_id)
    img_fn = image_fn(image_key_to_filename)

    chain_lines = []
    for msg in chain:
        time_str = msg.create_time.strftime("%H:%M:%S")
        speaker = _speaker_of(msg)
        rel = await _rel_of(msg)
        text = parse_content(msg.content).render(image_fn=img_fn)
        marker = "⭐" if msg.message_id == trigger_id else ""
        chain_lines.append(
            format_message_tag(
                speaker=speaker, rel=rel, time_str=time_str, body=text, marker=marker
            )
        )

    other_lines = []
    for msg in other:
        time_str = msg.create_time.strftime("%H:%M:%S")
        speaker = _speaker_of(msg)
        rel = await _rel_of(msg)
        text = parse_content(msg.content).render(image_fn=img_fn)
        other_lines.append(
            format_message_tag(
                speaker=speaker, rel=rel, time_str=time_str, body=text
            )
        )

    user_content = get_prompt("context_builder").compile(
        reply_chain="\n".join(chain_lines) if chain_lines else "（无回复链）",
        other_messages="\n".join(other_lines) if other_lines else "（无其他消息）",
    )

    content_blocks: list[ContentBlock] = [ContentBlock.from_text(user_content)]

    # Attach reply chain images as content blocks
    for msg in chain:
        parsed = parse_content(msg.content)
        for key in parsed.image_keys:
            fn = image_key_to_filename.get(key)
            url = image_key_to_url.get(key)
            if fn and url:
                content_blocks.append(ContentBlock.from_text(f"@{fn}:"))
                content_blocks.append(ContentBlock.from_image(url=url))

    return [Message(role=Role.USER, content=content_blocks)]


async def build_p2p_messages(
    messages: list[QuickSearchResult],
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
    current_persona_id: str = "",
) -> list[Message]:
    """Build P2P message list with full image content blocks.

    Task 3: 真人发言（USER 行）的文本块渲染成结构化标签，rel 按 common_user_id
    盖章（命中主人 → owner），用户字串全转义。赤尾自己的发言（ASSISTANT）认作她
    自己说的、不盖 rel、文本原样（不套对方署名标签）。图片块语义不变（多模态）。

    ``current_persona_id`` 只用于判定哪条历史是赤尾自己说的（``is_self``），
    与身份 rel 无关——rel 一律按 common_user_id 查主人。assistant 行 persona_id
    缺失（迁移前老数据）也算 self；只有 persona_id 明确等于另一个 persona 才非 self
    （修复 2：否则缺 persona_id 的老回复被错判成用户输入、归属错乱）。
    """
    result: list[Message] = []
    img_fn = image_fn(image_key_to_filename)

    for msg in messages:
        parsed = parse_content(msg.content)
        text_content = parsed.render(image_fn=img_fn)

        # Current persona's messages -> ASSISTANT; everything else -> USER.
        # 私聊里 assistant 就是赤尾自己——persona_id 缺失（迁移前老数据）也算 self；
        # 只有 persona_id 明确等于**另一个** persona 才非 self（修复 2）。否则缺 persona_id
        # 的老回复会被判非 self → 进 USER 分支 → 渲染成 from="我" 的 USER 标签、归属错乱。
        msg_persona_id = getattr(msg, "persona_id", None)
        is_self = msg.role == "assistant" and (
            not msg_persona_id or msg_persona_id == current_persona_id
        )
        role = Role.ASSISTANT if is_self else Role.USER

        content_blocks: list[ContentBlock] = []
        if text_content:
            if is_self:
                # 赤尾自己的话：ASSISTANT 角色已表明是她说的，文本原样、不套署名标签。
                content_blocks.append(ContentBlock.from_text(text_content))
            else:
                # 真人发言：结构化署名，rel 只来自 common_user_id 登记、正文转义。
                rel = await _rel_of(msg)
                time_str = msg.create_time.strftime("%H:%M:%S")
                content_blocks.append(
                    ContentBlock.from_text(
                        format_message_tag(
                            speaker=_speaker_of(msg),
                            rel=rel,
                            time_str=time_str,
                            body=text_content,
                        )
                    )
                )

        for key in parsed.image_keys:
            fn = image_key_to_filename.get(key)
            url = image_key_to_url.get(key)
            if fn and url:
                content_blocks.append(ContentBlock.from_text(f"@{fn}:"))
                content_blocks.append(ContentBlock.from_image(url=url))
            elif not fn:
                logger.warning(
                    "Image not registered: key=%s, msg=%s", key, msg.message_id
                )

        if not content_blocks:
            continue

        result.append(Message(role=role, content=content_blocks))

    return result
