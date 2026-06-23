"""Image collection + download permission cache (private to chat/).

Extracted from chat/context.py per Phase 6 v4 §3.1: keep the human-chat context
builder slim by moving the image processing pipeline into a focused module.
"""
from __future__ import annotations

import logging
import time

from app.capabilities.concurrency import fan_out_wait
from app.chat.content_parser import parse_content
from app.chat.quick_search import QuickSearchResult
from app.data.queries import find_group_download_permission
from app.infra.image import image_client

logger = logging.getLogger(__name__)

# Download permission cache: chat_id -> (allows, expire_monotonic)
_download_cache: dict[str, tuple[bool, float]] = {}
_CACHE_TTL = 600  # 10 min


async def allows_download(chat_id: str, chat_type: str) -> bool:
    """Check group download permission with in-memory cache."""
    if chat_type == "p2p":
        return True

    now = time.monotonic()
    cached = _download_cache.get(chat_id)
    if cached and cached[1] > now:
        return cached[0]

    try:
        setting = await find_group_download_permission(chat_id)
        allows = setting != "not_anyone"
    except Exception:
        logger.warning(
            "Download permission check failed for %s, defaulting to allow", chat_id
        )
        allows = True

    _download_cache[chat_id] = (allows, now + _CACHE_TTL)
    return allows


async def collect_images(
    results: list[QuickSearchResult],
    chat_type: str,
    bot_name: str = "",
) -> tuple[dict[str, str], dict[str, str]]:
    """Collect image URLs from message history.

    Returns (image_key -> url, image_key -> tos_file_name).

    ``bot_name`` is the bot that received the current turn's messages; it is
    forwarded to ``process_image`` so tool-service gets the ``X-App-Name`` it
    needs to download Lark images with the right bot credential. Without it the
    download is rejected with HTTP 422 and the user's image silently vanishes
    from the LLM context (trace dbde982e146840cc00610c393fc5820e).
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
        url_results = await fan_out_wait(
            [image_client.get_url(tos_file) for _, tos_file in cached_keys]
        )
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
        if not await allows_download(chat_id, chat_type):
            logger.info(
                "Group %s disallows download, skipping %d images",
                chat_id,
                len(uncached_keys),
            )
            uncached_keys = []

    # Uncached: full pipeline (Lark download -> compress -> TOS)
    if uncached_keys:
        if not bot_name:
            logger.warning(
                "collect_images missing bot_name: %d inbound image(s) cannot "
                "download (X-App-Name absent -> tool-service 422). Likely a stale "
                "payload / MQ replay where ChatRequest.bot_name was empty.",
                len(uncached_keys),
            )
        process_results = await fan_out_wait(
            [
                image_client.process_image(
                    key, msg_id if role == "user" else None, bot_name=bot_name
                )
                for key, msg_id, role in uncached_keys
            ]
        )
        for i, result in enumerate(process_results):
            key, msg_id, _ = uncached_keys[i]
            if isinstance(result, dict) and result:
                image_key_to_url[key] = result["url"]
                if result.get("file_name"):
                    image_key_to_file[key] = result["file_name"]
            else:
                logger.warning("Image processing failed: key=%s, msg=%s", key, msg_id)

    return image_key_to_url, image_key_to_file
