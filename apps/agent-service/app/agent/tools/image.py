"""Image reading and generation tools.

Merges the old image/read.py, image/generate.py, and image/processor.py.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain.tools import tool
from langgraph.runtime import get_runtime
from pydantic import Field

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error, upload_and_register

logger = logging.getLogger(__name__)


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
    context = get_runtime(AgentContext).context
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
) -> str | list[dict[str, Any]]:
    """生成图片。调用前必须先 load_skill("drawing") 加载画图指南并遵循其流程。"""
    context = get_runtime(AgentContext).context
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

    model_name = "default-generate-image-model"
    if context.get_feature("image_model"):
        model_name = context.get_feature("image_model")
        logger.info("Feature flag overrides image model to: %s", model_name)

    from app.agent.embedding import generate_image as _gen_image

    base64_images = await _gen_image(
        model_name,
        prompt=query,
        size=size,
        reference_images=reference_urls if reference_urls else None,
    )

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

    return content_blocks
