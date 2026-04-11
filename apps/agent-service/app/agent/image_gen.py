"""Image generation — text-to-image / image-to-image via Ark, OpenAI, or Gemini.

Public API:
  - generate_image() — dispatch to the correct backend based on provider config
"""

from __future__ import annotations

import base64
import logging
from math import gcd
from typing import Any

from app.agent.embedding import _create_ark_client
from app.agent.models import resolve_model_info

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
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
    info = await resolve_model_info(model_id)
    client_type = (info.get("client_type") or "openai").lower()

    if client_type == "ark":
        return await _generate_image_ark(info, prompt, size, reference_images)
    if client_type == "google":
        return await _generate_image_gemini(info, prompt, size, reference_images)
    # Default: OpenAI-compatible
    return await _generate_image_openai(info, prompt, size, reference_images)


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


async def _generate_image_ark(
    info: dict[str, Any],
    prompt: str,
    size: str,
    reference_images: list[str] | None,
) -> list[str]:
    """Image generation via Volcengine Ark SDK."""
    client = _create_ark_client(info)
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
        return [f"data:image/jpeg;base64,{img.b64_json}" for img in resp.data or []]
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
    http_client = None
    if info.get("use_proxy"):
        from app.infra.config import settings

        if settings.forward_proxy_url:
            import httpx

            http_client = httpx.AsyncClient(proxy=settings.forward_proxy_url)
            kwargs["http_client"] = http_client

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
        return [f"data:image/jpeg;base64,{img.b64_json}" for img in resp.data or []]
    finally:
        await client.close()
        if http_client:
            await http_client.aclose()


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

    response = await client.aio.models.generate_content(
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
