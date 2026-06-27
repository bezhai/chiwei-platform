"""Human-chat context builder — the source-message half of a real-person turn.

This is the **what** half of a real-person chat turn (decision 2 in the
proactive-chat spec): given a source ``message_id``, assemble everything the
shared render layer needs and pack it into a :class:`~app.chat.render.ChatTurnContext`.

It **owns all source-message-dependent work**, and only that:
  - fetch recent history via ``quick_search`` (keyed on the source message_id)
  - collect images, persist new TOS files, register them
  - derive trigger info (who / which chat) from the history tail
  - build the group / p2p LLM message shapes
  - resolve the persona bundle (identity / appearance / persona object)
  - assemble ``inner_context`` (scene + life state + pages) for this turn

The render layer (``app.chat.render.render_chat_turn``) takes the result and
never touches a source message. The life/proactive path (task 2) is a *separate*
context builder producing the same ``ChatTurnContext`` shape — it does not reuse
this one (its inputs are a life intent + chat_id history, no source message).

Image pipeline:
  1. feishu image_key -> process_image -> TOS URL
  2. TOS URL -> ImageRegistry.register -> N.png
  3. text references images as @N.png

Sub-modules (private to chat/):
  - _context_images.py: download permission cache + image collection
  - _context_messages.py: group / p2p LLM message list builders
"""

from __future__ import annotations

import logging
import time

from app.api.middleware import CHAT_PIPELINE_DURATION
from app.chat._context_images import collect_images
from app.chat._context_messages import build_group_messages, build_p2p_messages
from app.chat.quick_search import quick_search
from app.chat.render import ChatTurnContext
from app.domain.chat_events import CommonMessageContentSynced
from app.infra.image import ImageRegistry
from app.memory._persona import load_persona
from app.memory.context import build_inner_context
from app.runtime import emit

logger = logging.getLogger(__name__)


async def build_human_chat_context(
    message_id: str,
    *,
    persona_id: str,
    bot_name: str = "",
    channel: str | None = None,
    limit: int = 10,
) -> ChatTurnContext | None:
    """Build a render-ready context for a real-person chat turn.

    Returns a :class:`ChatTurnContext` packed with the LLM messages, image
    registry, chat address, resolved persona bundle, and assembled inner_context.
    Returns ``None`` when the source message resolves to no history — the caller
    treats that as "message not found" and emits the not-found segment.

    ``bot_name`` is the bot that received this turn's message; it flows down to
    ``collect_images`` → ``process_image`` so inbound Lark images download with
    the right bot credential (see ``collect_images`` for why it matters).
    """
    t_build_start = time.monotonic()
    l1_results = await quick_search(message_id=message_id, limit=limit)

    if not l1_results:
        logger.warning("No results found for message_id: %s", message_id)
        return None

    chat_type = l1_results[-1].chat_type or "p2p"
    chat_id = l1_results[0].chat_id or ""

    # --- Image processing ---
    image_key_to_url, image_key_to_file = await collect_images(
        l1_results, chat_type, bot_name=bot_name, channel=channel
    )

    # Persist new TOS files in background via dataflow (Phase 6 v4 Gap 5).
    if image_key_to_file:
        await emit(CommonMessageContentSynced(
            message_id=message_id,
            messages_json=[
                {"message_id": m.message_id, "content": m.content}
                for m in l1_results
            ],
            image_key_to_file=dict(image_key_to_file),
        ))

    # Register all images
    registry = ImageRegistry(message_id)
    image_key_to_filename: dict[str, str] = {}
    if image_key_to_url:
        keys_ordered = list(image_key_to_url.keys())
        urls_ordered = [image_key_to_url[k] for k in keys_ordered]
        filenames = await registry.register_batch(urls_ordered)
        for key, filename in zip(keys_ordered, filenames, strict=False):
            image_key_to_filename[key] = filename

    # Trigger info
    trigger_username = l1_results[-1].username or ""
    trigger_user_id = l1_results[-1].user_id or ""
    chat_name = l1_results[-1].chat_name or ""

    # Build messages. 群 / 私聊历史每条都结构化署名（rel 按 common_user_id 盖章，
    # 命中主人 → owner），所以两个 builder 都是 async。p2p 还要 current_persona_id
    # 判定哪条是赤尾自己说的（is_self），与身份 rel 无关。
    if chat_type == "group":
        messages = await build_group_messages(
            l1_results, message_id, image_key_to_url, image_key_to_filename,
        )
    else:
        messages = await build_p2p_messages(
            l1_results,
            image_key_to_url,
            image_key_to_filename,
            current_persona_id=persona_id,
        )

    chain_user_ids = list(
        dict.fromkeys(
            r.user_id for r in l1_results if r.role != "assistant" and r.user_id
        )
    )

    # Resolve persona bundle (identity + appearance + the persona object for
    # error messages). chat_node always supplies a non-empty persona_id, so this
    # is a direct load — the render layer never re-loads persona.
    persona = await load_persona(persona_id)

    # Assemble inner_context (scene + life state + relationship/day pages +
    # notebook). A failure here must not collapse the turn — fall back to "" and
    # let the render layer run with a thinner prompt.
    inner_context = ""
    try:
        inner_context = await build_inner_context(
            chat_id=chat_id,
            chat_type=chat_type,
            user_ids=chain_user_ids,
            trigger_user_id=trigger_user_id,
            trigger_username=trigger_username,
            chat_name=chat_name,
            persona_id=persona_id,
            channel=channel,
        )
    except Exception as e:
        logger.error("Failed to build inner context: %s", e)

    CHAT_PIPELINE_DURATION.labels(stage="context_build").observe(
        time.monotonic() - t_build_start
    )

    return ChatTurnContext(
        messages=messages,
        image_registry=registry,
        chat_id=chat_id,
        persona_id=persona_id,
        identity=persona.persona_lite,
        appearance=persona.appearance_detail,
        inner_context=inner_context,
        reply_style=persona.default_reply_style,
        persona=persona,
    )
