"""Embedding and image generation — the exception paths that bypass Agent.

These are the only LLM operations that do NOT go through the Agent class:
  - Embedding: text/multimodal vectorization via Volcengine Ark SDK
  - Image generation: via Ark, OpenAI, or Gemini depending on model config

Public API:
  - embed_dense()     — dense embedding (text, optionally multimodal)
  - embed_hybrid()    — dense + sparse embedding for hybrid retrieval
  - generate_image()  — text-to-image / image-to-image
  - InstructionBuilder — build instruction prefixes for doubao-embedding
  - Modality          — modality constants (text / image / text and image)

Data types:
  - SparseVector      — NamedTuple(indices, values)
  - HybridEmbedding   — dataclass(dense, sparse)
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from math import gcd
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class SparseVector(NamedTuple):
    """Sparse vector: parallel lists of token indices and weights."""

    indices: list[int]
    values: list[float]


@dataclass(slots=True)
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

    @staticmethod
    def for_cluster(target_modality: str, instruction: str) -> str:
        """Instruction for clustering / STS tasks."""
        return f"Target_modality: {target_modality}.\nInstruction:{instruction}\nQuery:"


# ---------------------------------------------------------------------------
# Internal: resolve model_id -> connection params
# ---------------------------------------------------------------------------


async def _resolve_model(model_id: str) -> dict[str, Any]:
    """Resolve model_id to provider connection parameters.

    Returns dict with: model_name, api_key, base_url, client_type, use_proxy.
    Raises ValueError on missing / inactive config.
    """
    from app.agent.models import _get_model_and_provider_info

    info = await _get_model_and_provider_info(model_id)
    if info is None:
        raise ValueError(f"model info not found: {model_id}")
    if not info.get("is_active", True):
        raise ValueError(f"model is disabled: {model_id}")

    required = ("api_key", "base_url", "model_name")
    missing = [f for f in required if not info.get(f)]
    if missing:
        raise ValueError(f"[{model_id}] missing config fields: {', '.join(missing)}")

    return info


# ---------------------------------------------------------------------------
# Internal: Ark embedding client (the only embedding provider)
# ---------------------------------------------------------------------------


async def _create_ark_client(info: dict[str, Any]) -> Any:
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

    info = await _resolve_model(model_id)
    client = await _create_ark_client(info)

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

    info = await _resolve_model(model_id)
    client = await _create_ark_client(info)
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


# ---------------------------------------------------------------------------
# Public: Image generation
# ---------------------------------------------------------------------------


async def generate_image(
    model_id: str,
    *,
    prompt: str,
    size: str,
    reference_images: list[str] | None = None,
) -> list[str]:
    """Generate images from a text prompt (and optional reference images).

    Dispatches to Ark, OpenAI, or Gemini based on provider's ``client_type``.

    Args:
        model_id: Internal model alias (e.g. ``"default-generate-image-model"``).
        prompt: Generation prompt.
        size: Size spec (e.g. ``"2048x2048"``, ``"1K"``, ``"2K"``, ``"4K"``).
        reference_images: Optional list of reference image URLs for img2img.

    Returns:
        List of ``data:<mime>;base64,<b64>`` encoded images.
    """
    info = await _resolve_model(model_id)
    client_type = (info.get("client_type") or "openai").lower()

    if client_type == "ark":
        return await _generate_image_ark(info, prompt, size, reference_images)
    if client_type == "google":
        return await _generate_image_gemini(info, prompt, size, reference_images)
    # Default: OpenAI-compatible
    return await _generate_image_openai(info, prompt, size, reference_images)


async def _generate_image_ark(
    info: dict[str, Any],
    prompt: str,
    size: str,
    reference_images: list[str] | None,
) -> list[str]:
    """Image generation via Volcengine Ark SDK."""
    client = await _create_ark_client(info)
    try:
        resp = await client.images.generate(
            model=info["model_name"],
            prompt=prompt,
            size=size,
            image=reference_images or None,
            response_format="b64_json",
            watermark=False,
            sequential_image_generation="disabled",
        )
        return [
            f"data:image/jpeg;base64,{img.b64_json}" for img in resp.data or []
        ]
    finally:
        await client.close()


async def _generate_image_openai(
    info: dict[str, Any],
    prompt: str,
    size: str,
    reference_images: list[str] | None,
) -> list[str]:
    """Image generation via OpenAI-compatible API."""
    from openai import AsyncOpenAI

    kwargs: dict[str, Any] = {
        "api_key": info["api_key"],
        "base_url": info["base_url"],
        "timeout": 60.0,
        "max_retries": 3,
    }
    if info.get("use_proxy"):
        from app.infra.config import settings

        if settings.forward_proxy_url:
            import httpx

            kwargs["http_client"] = httpx.AsyncClient(proxy=settings.forward_proxy_url)

    client = AsyncOpenAI(**kwargs)
    try:
        extra_body: dict[str, Any] = {
            "watermark": False,
            "sequential_image_generation": "disabled",
        }
        if reference_images:
            extra_body["image"] = reference_images

        resp = await client.images.generate(
            model=info["model_name"],
            response_format="b64_json",
            prompt=prompt,
            size=size,  # type: ignore[arg-type]
            n=1,
            extra_body=extra_body,
        )
        return [
            f"data:image/jpeg;base64,{img.b64_json}" for img in resp.data or []
        ]
    finally:
        await client.close()


def _parse_gemini_size(size: str) -> tuple[str, str]:
    """Parse size string to (aspect_ratio, image_size) for Gemini."""
    s = size.strip().upper()

    if "X" in s:
        try:
            w_str, h_str = s.split("X", 1)
            w, h = int(w_str), int(h_str)
            if w > 0 and h > 0:
                g = gcd(w, h)
                aspect_ratio = f"{w // g}:{h // g}"
                longest = max(w, h)
                if longest <= 1024:
                    image_size = "1K"
                elif longest <= 2048:
                    image_size = "2K"
                else:
                    image_size = "4K"
                return aspect_ratio, image_size
        except (ValueError, ZeroDivisionError):
            pass

    if s in {"1K", "2K", "4K"}:
        return "1:1", s

    return "1:1", "1K"


async def _generate_image_gemini(
    info: dict[str, Any],
    prompt: str,
    size: str,
    reference_images: list[str] | None,
) -> list[str]:
    """Image generation via Google Gemini generateContent API."""
    from google import genai
    from google.genai import types

    from app.infra.config import settings

    http_opts: dict[str, Any] = {}
    if info.get("base_url"):
        http_opts["base_url"] = info["base_url"]
    if info.get("use_proxy") and settings.forward_proxy_url:
        http_opts["client_args"] = {"proxy": settings.forward_proxy_url}

    client = genai.Client(
        api_key=info["api_key"],
        http_options=types.HttpOptions(**http_opts) if http_opts else None,
    )

    aspect_ratio, image_size = _parse_gemini_size(size)

    contents: list[types.Part | str] = []
    if reference_images:
        for url in reference_images:
            contents.append(types.Part.from_uri(file_uri=url, mime_type="image/*"))
    contents.append(prompt)

    response = client.models.generate_content(
        model=info["model_name"],
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            ),
        ),
    )

    if not response.candidates:
        raise RuntimeError("Gemini image generation returned no candidates")

    images: list[str] = []
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            mime = part.inline_data.mime_type or "image/png"
            b64 = base64.b64encode(part.inline_data.data).decode()
            images.append(f"data:{mime};base64,{b64}")

    if not images:
        raise RuntimeError("Gemini response contained no image data")

    return images
