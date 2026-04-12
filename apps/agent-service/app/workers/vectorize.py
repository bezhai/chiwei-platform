"""Vectorize worker — MQ consumer + cron scan for embedding messages.

Consumes the ``vectorize`` queue: for each message_id, fetch content,
generate hybrid + cluster embeddings, upsert into Qdrant.

Cron ``cron_scan_pending_messages`` sweeps pending messages into the queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

from aio_pika.abc import AbstractIncomingMessage

from app.agent.embedding import InstructionBuilder, embed_hybrid
from app.chat.content_parser import parse_content
from app.data.models import ConversationMessage
from app.data.queries import (
    find_group_download_permission,
    find_message_by_id,
    set_vector_status,
)
from app.data.queries import (
    scan_pending_messages as _crud_scan_pending,
)
from app.data.session import get_session
from app.infra.image import image_client
from app.infra.qdrant import qdrant
from app.infra.rabbitmq import VECTORIZE, current_lane, lane_queue, mq
from app.infra.redis import get_redis
from app.workers.common import cron_error_handler, mq_error_handler

logger = logging.getLogger(__name__)

# Concurrency control
CONCURRENCY_LIMIT = 10
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore  # noqa: PLW0603
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    return _semaphore


# ---------------------------------------------------------------------------
# Core: vectorize a single message
# ---------------------------------------------------------------------------


async def vectorize_message(message: ConversationMessage) -> bool:
    """Embed message content and write to Qdrant (recall + cluster collections).

    Returns True on success, False when content is empty (skip).
    """
    # 1. Parse content
    parsed = parse_content(message.content)
    image_keys = parsed.image_keys
    text_content = parsed.render()

    if not text_content and not image_keys:
        logger.info("Message %s content empty, skip vectorize", message.message_id)
        return False

    # 2. Download permission check
    if image_keys:
        async with get_session() as s:
            perm = await find_group_download_permission(s, message.chat_id)
        allows_download = perm != "only_owner"
        if not allows_download:
            logger.debug(
                "Group %s restricts download, skip %d images",
                message.chat_id,
                len(image_keys),
            )
            image_keys = []

    # 3. Download images
    image_base64_list: list[str] = []
    if image_keys:
        tasks = [
            image_client.download_image_as_base64(key, message.message_id, "chiwei")
            for key in image_keys
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        image_base64_list = [r for r in results if isinstance(r, str) and r]

    # 4. Post-download empty check
    if not text_content and not image_base64_list:
        logger.info(
            "Message %s images failed/skipped and no text, skip vectorize",
            message.message_id,
        )
        return False

    # 5. Build instructions
    modality = InstructionBuilder.detect_input_modality(text_content, image_base64_list)
    corpus_instructions = InstructionBuilder.for_corpus(modality)
    cluster_instructions = InstructionBuilder.for_cluster(
        target_modality=modality,
        instruction="Retrieve semantically similar content",
    )

    # 6. Generate embeddings
    from app.agent.embedding import embed_dense

    hybrid_task = embed_hybrid(
        "embedding-model",
        text=text_content or None,
        image_base64_list=image_base64_list or None,
        instructions=corpus_instructions,
    )
    cluster_task = embed_dense(
        "embedding-model",
        text=text_content or None,
        image_base64_list=image_base64_list or None,
        instructions=cluster_instructions,
    )
    hybrid_embedding, cluster_vector = await asyncio.gather(hybrid_task, cluster_task)

    # 7. Generate deterministic vector ID
    vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, message.message_id))

    # 8. Prepare payloads
    hybrid_payload = {
        "message_id": message.message_id,
        "user_id": message.user_id,
        "chat_id": message.chat_id,
        "timestamp": message.create_time,
        "root_message_id": message.root_message_id,
        "original_text": text_content,
    }
    cluster_payload = {
        "message_id": message.message_id,
        "user_id": message.user_id,
        "chat_id": message.chat_id,
        "timestamp": message.create_time,
    }

    # 9. Upsert into both collections
    results = await asyncio.gather(
        qdrant.upsert_hybrid_vectors(
            collection_name="messages_recall",
            point_id=vector_id,
            dense_vector=hybrid_embedding.dense,
            sparse_indices=hybrid_embedding.sparse.indices,
            sparse_values=hybrid_embedding.sparse.values,
            payload=hybrid_payload,
        ),
        qdrant.upsert_vectors(
            collection="messages_cluster",
            vectors=[cluster_vector],
            ids=[vector_id],
            payloads=[cluster_payload],
        ),
    )
    if not all(results):
        logger.error("Qdrant upsert partially failed for %s: %s", message.message_id, results)
        return False
    return True


# ---------------------------------------------------------------------------
# Single message processor (with concurrency + status tracking)
# ---------------------------------------------------------------------------


async def process_message(message_id: str) -> None:
    """Process one message with semaphore-based concurrency control."""
    async with _get_semaphore():
        try:
            async with get_session() as s:
                message = await find_message_by_id(s, message_id)
            if not message:
                logger.warning("Message %s not found, skip", message_id)
                return

            if message.vector_status in ("completed", "skipped"):
                logger.debug("Message %s already %s", message_id, message.vector_status)
                return

            success = await vectorize_message(message)

            status = "completed" if success else "skipped"
            async with get_session() as s:
                await set_vector_status(s, message_id, status)
            logger.info("Message %s vectorize: %s", message_id, status)

        except Exception as e:
            logger.error("Message %s vectorize failed: %s", message_id, e)
            async with get_session() as s:
                await set_vector_status(s, message_id, "failed")


# ---------------------------------------------------------------------------
# MQ consumer
# ---------------------------------------------------------------------------


@mq_error_handler()
async def handle_vectorize(message: AbstractIncomingMessage) -> None:
    """RabbitMQ consumer callback for vectorize queue."""
    async with message.process(requeue=False):
        body = json.loads(message.body)
        message_id = body.get("message_id")
        if not message_id:
            logger.warning("Vectorize message missing message_id, skip")
            return
        await process_message(message_id)


async def start_vectorize_consumer() -> None:
    """Connect MQ and start consuming the vectorize queue."""
    await mq.connect()
    await mq.declare_topology()
    lane = current_lane()
    queue = lane_queue(VECTORIZE.queue, lane)
    await mq.consume(queue, handle_vectorize)
    logger.info("Vectorize consumer started (queue=%s)", queue)


# ---------------------------------------------------------------------------
# Cron: sweep pending messages into vectorize queue
# ---------------------------------------------------------------------------

_SCAN_BATCH = 100
_SCAN_MAX = 1000
_SCAN_DAYS = 7


async def _scan_and_enqueue() -> int:
    """Scan pending messages from DB and publish to vectorize queue."""
    cutoff_time = datetime.now() - timedelta(days=_SCAN_DAYS)
    cutoff_ts = int(cutoff_time.timestamp() * 1000)

    total = 0
    offset = 0

    while total < _SCAN_MAX:
        async with get_session() as s:
            message_ids = await _crud_scan_pending(s, cutoff_ts, offset, _SCAN_BATCH)
        if not message_ids:
            break

        for mid in message_ids:
            await mq.publish(VECTORIZE, {"message_id": mid})
            total += 1

        logger.info("Enqueued %d pending messages for vectorize", len(message_ids))
        offset += _SCAN_BATCH

        if total < _SCAN_MAX:
            await asyncio.sleep(1)

    return total


@cron_error_handler()
async def cron_scan_pending_messages(ctx) -> None:
    """Cron: scan pending messages and push to vectorize queue.

    Uses a distributed lock to avoid duplicate scans.
    """
    redis = await get_redis()
    lock_key = "vectorize:pending_scan:lock"

    got = await redis.set(lock_key, "1", ex=300, nx=True)
    if not got:
        logger.info("Pending vectorize scan already running, skip")
        return

    try:
        logger.info("Scanning pending messages for vectorize...")
        count = await _scan_and_enqueue()
        logger.info("Pending vectorize scan done, enqueued %d messages", count)
    finally:
        await redis.delete(lock_key)
