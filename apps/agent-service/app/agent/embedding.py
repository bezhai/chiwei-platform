"""Embedding — text/multimodal vectorization via Volcengine Ark SDK.

Public API:
  - embed_dense()        — dense embedding (text, optionally multimodal)
  - embed_hybrid()       — dense + sparse embedding for hybrid retrieval
  - InstructionBuilder   — build instruction prefixes for doubao-embedding
  - Modality             — modality constants (text / image / text and image)

Data types:
  - SparseVector         — NamedTuple(indices, values)
  - HybridEmbedding      — frozen dataclass(dense, sparse)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, NamedTuple

from app.agent.models import resolve_model_info

logger = logging.getLogger(__name__)

# Embedding only supports Ark, which always needs base_url
_EMBEDDING_REQUIRED_FIELDS = ("api_key", "base_url", "model_name")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class SparseVector(NamedTuple):
    """Sparse vector: parallel lists of token indices and weights."""

    indices: list[int]
    values: list[float]


@dataclass(frozen=True, slots=True)
class HybridEmbedding:
    """Dense + sparse vectors for hybrid retrieval."""

    dense: list[float]
    sparse: SparseVector


# ---------------------------------------------------------------------------
# Modality constants
# ---------------------------------------------------------------------------


class Modality:
    """Input modality constants for doubao-embedding-vision."""

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    TEXT_AND_IMAGE = "text and image"
    TEXT_AND_VIDEO = "text and video"
    IMAGE_AND_VIDEO = "image and video"


# ---------------------------------------------------------------------------
# Instruction builder (doubao-embedding-vision specific)
# ---------------------------------------------------------------------------


class InstructionBuilder:
    """Build ``instructions`` strings for doubao-embedding-vision.

    Follows the model documentation:
      - Retrieval tasks (query/corpus distinction):
          Query:  ``Target_modality: {m}.\\nInstruction:{desc}\\nQuery:``
          Corpus: ``Instruction:Compress the {m} into one word.\\nQuery:``
      - Clustering / STS tasks (no distinction):
          ``Target_modality: {m}.\\nInstruction:{desc}\\nQuery:``
    """

    @staticmethod
    def detect_input_modality(
        text: str | None,
        images: list[str] | None,
    ) -> str:
        """Auto-detect modality from a single input sample."""
        has_text = bool(text and text.strip())
        has_image = bool(images)
        if has_text and has_image:
            return Modality.TEXT_AND_IMAGE
        if has_image:
            return Modality.IMAGE
        return Modality.TEXT  # fallback

    @staticmethod
    def combine_corpus_modalities(*modalities: str) -> str:
        """Combine multiple modality types with ``/`` separator."""
        return "/".join(modalities)

    @staticmethod
    def for_corpus(modality: str) -> str:
        """Corpus-side instruction for retrieval tasks."""
        return f"Instruction:Compress the {modality} into one word.\nQuery:"

    @staticmethod
    def for_query(target_modality: str, instruction: str) -> str:
        """Query-side instruction for retrieval tasks."""
        return f"Target_modality: {target_modality}.\nInstruction:{instruction}\nQuery:"

    # Clustering / STS uses the same format as query
    for_cluster = for_query


# ---------------------------------------------------------------------------
# Internal: Ark embedding client (the only embedding provider)
# ---------------------------------------------------------------------------


def _create_ark_client(info: dict[str, Any]) -> Any:
    """Create an AsyncArk client for embedding / image generation."""
    from volcenginesdkarkruntime import AsyncArk

    return AsyncArk(
        api_key=info["api_key"],
        base_url=info["base_url"],
        timeout=60.0,
        max_retries=3,
    )


def _build_multimodal_input(
    text: str | None, image_base64_list: list[str] | None
) -> list[dict[str, Any]]:
    """Build the ``input`` list for multimodal_embeddings.create()."""
    items: list[dict[str, Any]] = []
    if text:
        items.append({"type": "text", "text": text})
    for img in image_base64_list or []:
        items.append({"type": "image_url", "image_url": {"url": img}})
    return items


# ---------------------------------------------------------------------------
# Public: Embedding
# ---------------------------------------------------------------------------


async def embed_dense(
    model_id: str,
    *,
    text: str | None = None,
    image_base64_list: list[str] | None = None,
    instructions: str = "",
    dimensions: int = 1024,
) -> list[float]:
    """Generate a dense embedding vector.

    Supports multimodal input (text + images) via Volcengine Ark SDK.

    Args:
        model_id: Internal model alias (e.g. ``"embedding-model"``).
        text: Text content (optional if images provided).
        image_base64_list: Base64-encoded images (optional).
        instructions: Embedding instruction prefix.
        dimensions: Vector dimensions.

    Returns:
        Dense embedding vector as list of floats.
    """
    if not text and not image_base64_list:
        raise ValueError("embed_dense requires at least text or one image")

    info = await resolve_model_info(
        model_id, required_fields=_EMBEDDING_REQUIRED_FIELDS
    )
    client = _create_ark_client(info)

    try:
        input_list = _build_multimodal_input(text, image_base64_list)
        resp = await client.multimodal_embeddings.create(
            model=info["model_name"],
            input=input_list,
            dimensions=dimensions,
            encoding_format="float",
            extra_body={"instructions": instructions},
        )
        return resp.data.embedding
    finally:
        await client.close()


async def embed_hybrid(
    model_id: str,
    *,
    text: str | None = None,
    image_base64_list: list[str] | None = None,
    instructions: str = "",
    dimensions: int = 1024,
) -> HybridEmbedding:
    """Generate hybrid embedding (dense + sparse) for retrieval.

    Strategy:
      - Text-only: single request returns both dense and sparse vectors.
      - With images: two requests — multimodal dense, then text-only sparse.

    Args:
        model_id: Internal model alias (e.g. ``"embedding-model"``).
        text: Text content (optional if images provided).
        image_base64_list: Base64-encoded images (optional).
        instructions: Embedding instruction prefix.
        dimensions: Dense vector dimensions.

    Returns:
        HybridEmbedding with dense and sparse vectors.
    """
    if not text and not image_base64_list:
        raise ValueError("embed_hybrid requires at least text or one image")

    info = await resolve_model_info(
        model_id, required_fields=_EMBEDDING_REQUIRED_FIELDS
    )
    client = _create_ark_client(info)
    model_name = info["model_name"]

    try:
        has_images = bool(image_base64_list)

        if not has_images and text:
            # Text-only: one request for both dense + sparse
            text_input = [{"type": "text", "text": text}]
            resp = await client.multimodal_embeddings.create(
                model=model_name,
                input=text_input,
                dimensions=dimensions,
                encoding_format="float",
                extra_body={
                    "instructions": instructions,
                    "sparse_embedding": {"type": "enabled"},
                },
            )
            dense = resp.data.embedding
            raw_sparse: Any = resp.data.sparse_embedding or []
        else:
            # Multimodal: dense from text+images, sparse from text only
            dense_input = _build_multimodal_input(text, image_base64_list)
            dense_resp = await client.multimodal_embeddings.create(
                model=model_name,
                input=dense_input,
                dimensions=dimensions,
                encoding_format="float",
                extra_body={"instructions": instructions},
            )
            dense = dense_resp.data.embedding

            raw_sparse = []
            if text:
                text_input = [{"type": "text", "text": text}]
                sparse_resp = await client.multimodal_embeddings.create(
                    model=model_name,
                    input=text_input,
                    dimensions=dimensions,
                    encoding_format="float",
                    extra_body={
                        "instructions": instructions,
                        "sparse_embedding": {"type": "enabled"},
                    },
                )
                raw_sparse = sparse_resp.data.sparse_embedding or []

        # Convert Volcengine SparseEmbedding objects to SparseVector
        if raw_sparse:
            indices = [item.index for item in raw_sparse]
            values = [item.value for item in raw_sparse]
        else:
            indices = []
            values = []

        return HybridEmbedding(
            dense=dense,
            sparse=SparseVector(indices=indices, values=values),
        )
    finally:
        await client.close()
