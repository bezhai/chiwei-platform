"""Image reading and generation tools.

Merges the old image/read.py, image/generate.py, and image/processor.py.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import tool_error, upload_and_register

logger = logging.getLogger(__name__)

ImageQuality = Literal["high", "normal"]

_DEFAULT_IMAGE_QUALITY: ImageQuality = "high"
_IMAGE_MODEL_BY_QUALITY: dict[ImageQuality, str] = {
    "high": "generate-image-high-model",
    "normal": "generate-image-normal-model",
}


# =========================================================================
# Public tools
# =========================================================================


@tool
@tool_error("查看图片失败")
async def read_images(
    filenames: Annotated[
        list[str],
        Field(description='要查看的图片文件名列表，如 ["3.png", "5.png"]'),
    ],
) -> str | list[dict[str, Any]]:
    """查看指定的图片内容。对话中提到但未直接展示的图片，可以用此工具查看。

    Args:
        filenames: 图片文件名列表（如 ["3.png", "5.png"]）
    """
    context = get_context()
    registry = context.image_registry

    if not registry:
        return "当前对话没有可用的图片"

    content_blocks: list[dict[str, Any]] = []
    not_found: list[str] = []

    for filename in filenames:
        url = await registry.resolve(filename)
        if url:
            content_blocks.append({"type": "text", "text": f"@{filename}:"})
            content_blocks.append({"type": "image_url", "image_url": {"url": url}})
        else:
            not_found.append(filename)

    if not_found:
        logger.warning("Images not found: %s", not_found)

    if not content_blocks:
        return f"未找到图片: {', '.join(not_found)}"

    if not_found:
        content_blocks.insert(
            0, {"type": "text", "text": f"未找到: {', '.join(not_found)}"}
        )

    return content_blocks


@tool
@tool_error("图片生成失败")
async def generate_image(
    query: Annotated[
        str,
        Field(description="英文生成图片提示词。按 drawing skill 指南撰写。"),
    ],
    size: Annotated[
        str,
        Field(description="图片尺寸。可选值：1K、2K、4K 或像素值如 2048x2048"),
    ] = "2048x2048",
    image_list: Annotated[
        list[str] | None,
        Field(description='参考图片列表，使用 @N.png 文件名，如 ["4.png", "5.png"]'),
    ] = None,
    quality: Annotated[
        ImageQuality,
        Field(description="生成质量。high 更精细；normal 更快更稳定。不要询问或提及具体模型。"),
    ] = _DEFAULT_IMAGE_QUALITY,
) -> str | list[dict[str, Any]]:
    """生成图片。调用前必须先 load_skill("drawing") 加载画图指南并遵循其流程。"""
    context = get_context()
    registry = context.image_registry

    # Resolve reference images from registry
    reference_urls: list[str] = []
    if image_list and registry:
        for filename in image_list:
            url = await registry.resolve(filename)
            if url:
                reference_urls.append(url)
            else:
                logger.warning("Reference image not found: %s", filename)

    if reference_urls:
        logger.info("Using %d reference images", len(reference_urls))

    logger.info("Image generation request: %s", query)

    from app.agent.image_gen import generate_image as _gen_image

    base64_images: list[str] = []
    last_error: Exception | None = None
    for candidate_label, model_name in _model_candidates(
        quality,
        image_model_override=context.get_feature("image_model"),
    ):
        try:
            base64_images = await _gen_image(
                model_name,
                prompt=query,
                size=size,
                reference_images=reference_urls if reference_urls else None,
            )
            if base64_images:
                if candidate_label not in {quality, "override"}:
                    logger.info("Image generation fell back to %s quality", candidate_label)
                break
            last_error = RuntimeError("image generation returned no images")
            logger.warning("Image generation returned no images for %s", candidate_label)
        except Exception as exc:
            last_error = exc
            logger.warning("Image generation failed for %s: %s", candidate_label, exc)
    else:
        raise RuntimeError("图片生成服务暂时不可用") from last_error

    content_blocks: list[dict[str, Any]] = []
    filenames: list[str] = []

    for b64 in base64_images:
        tos_url, filename = await upload_and_register(
            source_type="base64",
            data=b64,
            registry=registry,
        )
        if not filename:
            logger.error("Image upload to TOS failed")
            continue

        filenames.append(filename)
        content_blocks.append({"type": "text", "text": f"生成了图片: @{filename}"})
        content_blocks.append({"type": "text", "text": f"@{filename}:"})
        content_blocks.append({"type": "image_url", "image_url": {"url": tos_url}})

    if not content_blocks:
        return "图片生成失败，请稍后重试"

    # Remind model to include image markdown — this is the last text
    # before the model generates its response, so it gets full attention.
    refs = " ".join(f"![描述]({f})" for f in filenames)
    content_blocks.append(
        {"type": "text", "text": f"⚠️ 用户看不到上面的图片。你的回复里必须包含 {refs} 才能展示给用户。"}
    )

    return content_blocks


def _quality_candidates(quality: str) -> list[ImageQuality]:
    if quality == "high":
        return ["high", "normal"]
    if quality == "normal":
        return ["normal"]
    raise ValueError("quality must be high or normal")


def _model_candidates(
    quality: str,
    *,
    image_model_override: Any = None,
) -> list[tuple[str, str]]:
    if image_model_override:
        return [("override", str(image_model_override))]
    return [
        (candidate, _IMAGE_MODEL_BY_QUALITY[candidate])
        for candidate in _quality_candidates(quality)
    ]
