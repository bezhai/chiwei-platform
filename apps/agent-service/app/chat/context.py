"""Chat context builder — assemble LLM messages from DB history.

Responsibilities:
  - Fetch recent history via quick_search
  - Process images (cached TOS URL / full pipeline)
  - Build group or P2P message lists with image registry
  - Detect proactive scan messages and extract stimulus

Image pipeline:
  1. feishu image_key -> process_image -> TOS URL
  2. TOS URL -> ImageRegistry.register -> N.png
  3. text references images as @N.png
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select

from app.agent.prompts import get_prompt
from app.data.models import ConversationMessage
from app.data.queries import find_group_download_permission
from app.data.session import async_session, get_session
from app.infra.image import ImageRegistry, image_client
from app.infra.redis import get_redis
from app.services.content_parser import parse_content, update_tos_files
from app.services.quick_search import QuickSearchResult, quick_search

logger = logging.getLogger(__name__)

PROACTIVE_USER_ID = "__proactive__"

# ContextVars for proactive scan state (read by pipeline.py)
is_proactive_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_proactive", default=False
)
proactive_stimulus_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "proactive_stimulus", default=""
)

# Download permission cache: chat_id -> (allows, expire_monotonic)
_download_cache: dict[str, tuple[bool, float]] = {}
_CACHE_TTL = 600  # 10 min


@dataclass
class ChatContext:
    """Assembled chat context returned by ``build_chat_context``."""

    messages: list[HumanMessage | AIMessage]
    image_registry: ImageRegistry | None
    chat_id: str
    trigger_username: str
    chat_type: str
    trigger_user_id: str
    chat_name: str
    chain_user_ids: list[str]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def build_chat_context(
    message_id: str,
    current_persona_id: str = "",
    limit: int = 10,
) -> ChatContext:
    """Build full chat context for the main agent.

    Returns a ``ChatContext`` dataclass with all fields the pipeline needs.
    """
    l1_results = await quick_search(message_id=message_id, limit=limit)

    if not l1_results:
        logger.warning("No results found for message_id: %s", message_id)
        return ChatContext([], None, "", "", "p2p", "", "", [])

    chat_type = l1_results[-1].chat_type or "p2p"

    # --- Proactive: filter synthetic messages ---
    proactive_msgs = [m for m in l1_results if m.user_id == PROACTIVE_USER_ID]
    is_proactive = len(proactive_msgs) > 0
    proactive_stimulus = ""
    proactive_target_id = ""

    if proactive_msgs:
        l1_results = [m for m in l1_results if m.user_id != PROACTIVE_USER_ID]
        latest = proactive_msgs[-1]
        proactive_stimulus = parse_content(latest.content).render()
        proactive_target_id = latest.reply_message_id or ""
        if not l1_results:
            logger.warning("proactive scan: no real messages after filtering")
            return ChatContext([], None, "", "", "group", "", "", [])

    is_proactive_var.set(is_proactive)
    proactive_stimulus_var.set(proactive_stimulus)

    # --- Image processing ---
    image_key_to_url, image_key_to_file = await _collect_images(l1_results, chat_type)

    # Persist new TOS files in background
    if image_key_to_file:
        asyncio.create_task(_persist_tos_files(l1_results, image_key_to_file))

    # Register all images
    redis = await get_redis()
    registry = ImageRegistry(message_id, redis)
    image_key_to_filename: dict[str, str] = {}
    if image_key_to_url:
        keys_ordered = list(image_key_to_url.keys())
        urls_ordered = [image_key_to_url[k] for k in keys_ordered]
        filenames = await registry.register_batch(urls_ordered)
        for key, filename in zip(keys_ordered, filenames, strict=False):
            image_key_to_filename[key] = filename

    # Trigger info
    if is_proactive:
        trigger_username = ""
        trigger_user_id = ""
        chat_name = l1_results[-1].chat_name or "" if l1_results else ""
        effective_trigger_id = proactive_target_id or (
            l1_results[-1].message_id if l1_results else message_id
        )
    else:
        trigger_username = l1_results[-1].username or ""
        trigger_user_id = l1_results[-1].user_id or ""
        chat_name = l1_results[-1].chat_name or ""
        effective_trigger_id = message_id

    # Build messages
    if chat_type == "group":
        messages = _build_group_messages(
            l1_results, effective_trigger_id, image_key_to_url, image_key_to_filename
        )
    else:
        messages = _build_p2p_messages(
            l1_results,
            image_key_to_url,
            image_key_to_filename,
            current_persona_id=current_persona_id,
        )

    chain_user_ids = list(
        {r.user_id for r in l1_results if r.role != "assistant" and r.user_id}
    )

    return ChatContext(
        messages=messages,
        image_registry=registry,
        chat_id=l1_results[0].chat_id or "",
        trigger_username=trigger_username,
        chat_type=chat_type,
        trigger_user_id=trigger_user_id,
        chat_name=chat_name,
        chain_user_ids=chain_user_ids,
    )


# ---------------------------------------------------------------------------
# Image collection
# ---------------------------------------------------------------------------


async def _allows_download(chat_id: str, chat_type: str) -> bool:
    """Check group download permission with in-memory cache."""
    if chat_type == "p2p":
        return True

    now = time.monotonic()
    cached = _download_cache.get(chat_id)
    if cached and cached[1] > now:
        return cached[0]

    try:
        async with get_session() as s:
            setting = await find_group_download_permission(s, chat_id)
        allows = setting != "not_anyone"
    except Exception:
        logger.warning(
            "Download permission check failed for %s, defaulting to allow", chat_id
        )
        allows = True

    _download_cache[chat_id] = (allows, now + _CACHE_TTL)
    return allows


async def _collect_images(
    results: list[QuickSearchResult],
    chat_type: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Collect image URLs from message history.

    Returns (image_key -> url, image_key -> tos_file_name).
    """
    cached_keys: list[tuple[str, str]] = []  # (image_key, tos_file)
    uncached_keys: list[tuple[str, str, str]] = []  # (image_key, message_id, role)

    for msg in results:
        parsed = parse_content(msg.content)
        for key in parsed.image_keys:
            if key.startswith("@"):
                continue
            tos_file = parsed.tos_files.get(key)
            if tos_file:
                cached_keys.append((key, tos_file))
            else:
                uncached_keys.append((key, msg.message_id, msg.role))

    image_key_to_url: dict[str, str] = {}
    image_key_to_file: dict[str, str] = {}

    # Cached images: sign URLs only
    if cached_keys:
        tasks = [image_client.get_url(tos_file) for _, tos_file in cached_keys]
        url_results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(url_results):
            key, tos_file = cached_keys[i]
            if isinstance(result, str) and result:
                image_key_to_url[key] = result
                image_key_to_file[key] = tos_file
            else:
                uncached_keys.append((key, "", ""))
                logger.warning("TOS URL sign failed, falling back: %s", key)

    # Permission check before downloading
    if uncached_keys:
        chat_id = results[0].chat_id or ""
        if not await _allows_download(chat_id, chat_type):
            logger.info(
                "Group %s disallows download, skipping %d images",
                chat_id,
                len(uncached_keys),
            )
            uncached_keys = []

    # Uncached: full pipeline (Lark download -> compress -> TOS)
    if uncached_keys:
        tasks = [
            image_client.process_image(key, msg_id if role == "user" else None)
            for key, msg_id, role in uncached_keys
        ]
        process_results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(process_results):
            key, msg_id, _ = uncached_keys[i]
            if isinstance(result, dict) and result:
                image_key_to_url[key] = result["url"]
                if result.get("file_name"):
                    image_key_to_file[key] = result["file_name"]
            else:
                logger.warning("Image processing failed: key=%s, msg=%s", key, msg_id)

    return image_key_to_url, image_key_to_file


# ---------------------------------------------------------------------------
# TOS file persistence (background)
# ---------------------------------------------------------------------------


async def _persist_tos_files(
    messages: list[QuickSearchResult],
    image_key_to_file: dict[str, str],
) -> None:
    """Write tos_file back into message content items (background task)."""
    try:
        msg_updates: dict[str, dict[str, str]] = {}
        for msg in messages:
            parsed = parse_content(msg.content)
            new_mappings = {}
            for key in parsed.image_keys:
                if key in image_key_to_file and key not in parsed.tos_files:
                    new_mappings[key] = image_key_to_file[key]
            if new_mappings:
                msg_updates[msg.message_id] = new_mappings

        if not msg_updates:
            return

        async with async_session() as session:
            for mid, mapping in msg_updates.items():
                row = await session.scalar(
                    select(ConversationMessage).where(
                        ConversationMessage.message_id == mid
                    )
                )
                if row:
                    updated = update_tos_files(row.content, mapping)
                    if updated:
                        row.content = updated
            await session.commit()
            logger.info("tos_file persisted for %d messages", len(msg_updates))
    except Exception:
        logger.warning("tos_file persistence failed", exc_info=True)


# ---------------------------------------------------------------------------
# Message list builders
# ---------------------------------------------------------------------------


def _image_fn(
    image_key_to_filename: dict[str, str],
):
    """Return an image render function for parse_content.render()."""

    def _fn(_i: int, key: str) -> str:
        fn = image_key_to_filename.get(key)
        return f"@{fn}" if fn else "[图片]"

    return _fn


def _extract_reply_chain(
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


def _build_group_messages(
    messages: list[QuickSearchResult],
    trigger_id: str,
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
) -> list[HumanMessage | AIMessage]:
    """Build group chat message list.

    Reply chain messages include image content blocks; other messages
    reference images as @N.png in text only.
    """
    chain, other = _extract_reply_chain(messages, trigger_id)
    img_fn = _image_fn(image_key_to_filename)

    chain_lines = []
    for msg in chain:
        time_str = msg.create_time.strftime("%H:%M:%S")
        username = msg.username or "未知用户"
        text = parse_content(msg.content).render(image_fn=img_fn)
        marker = " ⭐" if msg.message_id == trigger_id else ""
        chain_lines.append(f"[{time_str}] {username}: {text}{marker}")

    other_lines = []
    for msg in other:
        time_str = msg.create_time.strftime("%H:%M:%S")
        username = msg.username or "未知用户"
        text = parse_content(msg.content).render(image_fn=img_fn)
        other_lines.append(f"[{time_str}] {username}: {text}")

    user_content = get_prompt("context_builder").compile(
        reply_chain="\n".join(chain_lines) if chain_lines else "（无回复链）",
        other_messages="\n".join(other_lines) if other_lines else "（无其他消息）",
    )

    content_blocks: list = [{"type": "text", "text": user_content}]

    # Attach reply chain images as content blocks
    for msg in chain:
        parsed = parse_content(msg.content)
        for key in parsed.image_keys:
            fn = image_key_to_filename.get(key)
            url = image_key_to_url.get(key)
            if fn and url:
                content_blocks.append({"type": "text", "text": f"@{fn}:"})
                content_blocks.append({"type": "image", "url": url})

    return [HumanMessage(content_blocks=content_blocks)]  # type: ignore[arg-type]


def _build_p2p_messages(
    messages: list[QuickSearchResult],
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
    current_persona_id: str = "",
) -> list[HumanMessage | AIMessage]:
    """Build P2P message list with full image content blocks."""
    result: list[HumanMessage | AIMessage] = []
    img_fn = _image_fn(image_key_to_filename)

    for msg in messages:
        parsed = parse_content(msg.content)
        text_content = parsed.render(image_fn=img_fn)

        content_blocks: list = []
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        for key in parsed.image_keys:
            fn = image_key_to_filename.get(key)
            url = image_key_to_url.get(key)
            if fn and url:
                content_blocks.append({"type": "text", "text": f"@{fn}:"})
                content_blocks.append({"type": "image", "url": url})
            elif not fn:
                logger.warning(
                    "Image not registered: key=%s, msg=%s", key, msg.message_id
                )

        if not content_blocks:
            continue

        # Current persona's messages -> AIMessage; everything else -> HumanMessage
        msg_persona_id = getattr(msg, "persona_id", None)
        is_self = (
            msg.role == "assistant"
            and bool(msg_persona_id)
            and msg_persona_id == current_persona_id
        )
        if is_self:
            result.append(AIMessage(content_blocks=content_blocks))  # type: ignore[arg-type]
        else:
            result.append(HumanMessage(content_blocks=content_blocks))  # type: ignore[arg-type]

    return result
