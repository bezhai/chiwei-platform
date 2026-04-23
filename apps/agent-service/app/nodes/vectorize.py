"""vectorize @node: lift a ``Message`` into a ``Fragment`` (or ``None``).

Replaces the embed half of ``app.workers.vectorize.vectorize_message``:
parse content, permission-check images, download base64s, run hybrid +
dense-cluster embeddings in parallel, and pack the dual payloads that
``save_fragment`` will hand to the two qdrant collections.

Returns ``None`` in two skip scenarios, identical to the legacy worker:
  1. ``text_content`` and ``image_keys`` both empty before download;
  2. ``text_content`` empty and every image download failed/skipped.

The runtime drops ``None`` results before the durable edge, so the
downstream ``save_fragment`` @node never sees them.

Status write-back to ``conversation_messages.vector_status`` is *not*
performed here. The legacy ``process_message`` wrapper continues to own
status tracking until T1.10 retires ``app/workers/vectorize.py`` and the
runtime's persist layer takes over. This @node stays pure: Message in,
Fragment | None out.
"""
from __future__ import annotations

import asyncio
import logging

from app.capabilities.embed import EmbedderClient
from app.chat.content_parser import parse_content
from app.data.queries import find_group_download_permission
from app.data.session import get_session
from app.domain.fragment import Fragment
from app.domain.message import Message
from app.infra.image import image_client
from app.nodes._ids import vector_id_for
from app.runtime.node import node

# Late import to avoid pulling the heavy embedding client at module load
# when only the InstructionBuilder constants are needed.
from app.agent.embedding import InstructionBuilder

logger = logging.getLogger(__name__)

# One embedder per process — mirrors the pattern used by save_fragment.
embedder = EmbedderClient(model_id="embedding-model")


@node
async def vectorize(msg: Message) -> Fragment | None:
    # 1. Parse content
    parsed = parse_content(msg.content)
    text_content = parsed.render()
    image_keys = parsed.image_keys

    # 2. Early return A: nothing to embed
    if not text_content and not image_keys:
        logger.info("vectorize: message=%s empty, skip", msg.message_id)
        return None

    # 3. Permission check — drop images for "only_owner" groups
    if image_keys:
        async with get_session() as s:
            perm = await find_group_download_permission(s, msg.chat_id)
        if perm == "only_owner":
            logger.debug(
                "vectorize: chat=%s download restricted, drop %d image(s)",
                msg.chat_id,
                len(image_keys),
            )
            image_keys = []

    # 4. Download images (best-effort, filter failures)
    image_base64_list: list[str] = []
    if image_keys:
        tasks = [
            image_client.download_image_as_base64(key, msg.message_id, "chiwei")
            for key in image_keys
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        image_base64_list = [r for r in results if isinstance(r, str) and r]

    # 5. Early return B: no text and every image failed/skipped
    if not text_content and not image_base64_list:
        logger.info(
            "vectorize: message=%s text+images all empty/failed, skip",
            msg.message_id,
        )
        return None

    # 6. Build modality-aware instructions (two different strings)
    modality = InstructionBuilder.detect_input_modality(text_content, image_base64_list)
    corpus_instructions = InstructionBuilder.for_corpus(modality)
    cluster_instructions = InstructionBuilder.for_cluster(
        target_modality=modality,
        instruction="Retrieve semantically similar content",
    )

    # 7. Hybrid + cluster embeddings in parallel
    hybrid_emb, cluster_dense = await asyncio.gather(
        embedder.hybrid(
            text=text_content or None,
            image_base64_list=image_base64_list or None,
            instructions=corpus_instructions,
        ),
        embedder.dense(
            text=text_content or None,
            image_base64_list=image_base64_list or None,
            instructions=cluster_instructions,
        ),
    )

    # 8. Deterministic point id (stable across retries)
    vector_id = vector_id_for(msg.message_id)

    # 9. Dual-payload shapes — recall keeps root + original_text, cluster does not
    recall_payload = {
        "message_id": msg.message_id,
        "user_id": msg.user_id,
        "chat_id": msg.chat_id,
        "timestamp": msg.create_time,
        "root_message_id": msg.root_message_id,
        "original_text": text_content,
    }
    cluster_payload = {
        "message_id": msg.message_id,
        "user_id": msg.user_id,
        "chat_id": msg.chat_id,
        "timestamp": msg.create_time,
    }

    return Fragment(
        fragment_id=vector_id,
        message_id=msg.message_id,
        chat_id=msg.chat_id,
        dense=hybrid_emb.dense,
        sparse={
            "indices": hybrid_emb.sparse.indices,
            "values": hybrid_emb.sparse.values,
        },
        dense_cluster=cluster_dense,
        recall_payload=recall_payload,
        cluster_payload=cluster_payload,
    )
