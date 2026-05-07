"""Chat context builder — assemble LLM messages from DB history.

Responsibilities (orchestrator):
  - Fetch recent history via quick_search
  - Detect proactive scan messages and extract stimulus
  - Delegate image collection to _context_images.collect_images
  - Delegate message-shape construction to _context_messages.build_*
  - Emit ConversationMessageContentSynced for any new TOS files

Image pipeline:
  1. feishu image_key -> process_image -> TOS URL
  2. TOS URL -> ImageRegistry.register -> N.png
  3. text references images as @N.png

Sub-modules (private to chat/):
  - _context_images.py: download permission cache + image collection
  - _context_messages.py: group / p2p LLM message list builders
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage

from app.chat._context_images import collect_images
from app.chat._context_messages import build_group_messages, build_p2p_messages
from app.chat.content_parser import parse_content
from app.chat.quick_search import quick_search
from app.domain.chat_events import ConversationMessageContentSynced
from app.infra.image import ImageRegistry
from app.infra.redis import get_redis
from app.runtime import emit

logger = logging.getLogger(__name__)

PROACTIVE_USER_ID = "__proactive__"

# ContextVars for proactive scan state (read by pipeline.py)
is_proactive_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_proactive", default=False
)
proactive_stimulus_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "proactive_stimulus", default=""
)


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
    # Reset ContextVars before any early return to prevent stale values
    is_proactive_var.set(False)
    proactive_stimulus_var.set("")

    l1_results = await quick_search(message_id=message_id, limit=limit)

    if not l1_results:
        logger.warning("No results found for message_id: %s", message_id)
        return ChatContext([], None, "", "", "p2p", "", "", [])

    chat_type = l1_results[-1].chat_type or "p2p"

    current_msg = next((m for m in l1_results if m.message_id == message_id), None)
    is_proactive = bool(current_msg and current_msg.user_id == PROACTIVE_USER_ID)
    proactive_stimulus = ""
    proactive_target_id = ""

    # Synthetic proactive triggers are internal bookkeeping and should never
    # appear in the visible chat history, regardless of the current trigger type.
    l1_results = [m for m in l1_results if m.user_id != PROACTIVE_USER_ID]

    if is_proactive:
        if not current_msg:
            logger.warning("proactive trigger missing from quick_search: %s", message_id)
            return ChatContext([], None, "", "", "group", "", "", [])
        proactive_stimulus = parse_content(current_msg.content).render()
        proactive_target_id = current_msg.reply_message_id or ""
        if not l1_results:
            logger.warning("proactive scan: no real messages after filtering")
            return ChatContext([], None, "", "", "group", "", "", [])

    is_proactive_var.set(is_proactive)
    proactive_stimulus_var.set(proactive_stimulus)

    # --- Image processing ---
    image_key_to_url, image_key_to_file = await collect_images(l1_results, chat_type)

    # Persist new TOS files in background via dataflow (Phase 6 v4 Gap 5).
    if image_key_to_file:
        await emit(ConversationMessageContentSynced(
            message_id=message_id,
            messages_json=[
                {"message_id": m.message_id, "content": m.content}
                for m in l1_results
            ],
            image_key_to_file=dict(image_key_to_file),
        ))

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
        messages = build_group_messages(
            l1_results, effective_trigger_id, image_key_to_url, image_key_to_filename
        )
    else:
        messages = build_p2p_messages(
            l1_results,
            image_key_to_url,
            image_key_to_filename,
            current_persona_id=current_persona_id,
        )

    chain_user_ids = list(
        dict.fromkeys(
            r.user_id for r in l1_results if r.role != "assistant" and r.user_id
        )
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
