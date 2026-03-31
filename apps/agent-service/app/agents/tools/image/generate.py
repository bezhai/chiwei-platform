"""图片生成工具"""

import logging
from typing import Annotated, Any

from langchain.tools import tool
from langgraph.runtime import get_runtime
from pydantic import Field

from app.agents.clients import create_client
from app.agents.core.context import AgentContext

logger = logging.getLogger(__name__)


@tool
async def generate_image(
    query: Annotated[
        str,
        Field(
            description="英文生成图片提示词。按 drawing skill 指南撰写。"
        ),
    ],
    size: Annotated[
        str,
        Field(description="图片尺寸。可选值：1K、2K、4K 或像素值如 2048x2048"),
    ] = "2048x2048",
    image_list: Annotated[
        list[str] | None,
        Field(description="参考图片列表，使用 @N.png 文件名，如 [\"4.png\", \"5.png\"]"),
    ] = None,
) -> str | list[dict[str, Any]]:
    """
    生成图片。调用前必须先 load_skill("drawing") 加载画图指南并遵循其流程。
    """
    try:
        context = get_runtime(AgentContext).context
        registry = context.media.registry

        # Resolve reference images from registry
        reference_urls = []
        if image_list and registry:
            for filename in image_list:
                url = await registry.resolve(filename)
                if url:
                    reference_urls.append(url)
                else:
                    logger.warning(f"参考图片未找到: {filename}")

        if reference_urls:
            logger.info(f"使用参考图片: {len(reference_urls)} 张")

        logger.info(f"生成图片请求: {query}")

        model_name = "default-generate-image-model"
        if context.features.get("image_model"):
            model_name = context.features.get("image_model")
            logger.info(f"灰度配置覆盖图片模型为: {model_name}")

        async with await create_client(model_name) as client:
            base64_images = await client.generate_image(
                prompt=query,
                size=size,
                reference_images=reference_urls if reference_urls else None,
            )

            # Upload to TOS and register
            from app.clients.image_client import image_client

            content_blocks: list[dict[str, Any]] = []
            filenames: list[str] = []

            for b64 in base64_images:
                tos_url = await image_client.upload_to_tos("base64", b64)
                if not tos_url:
                    logger.error("图片上传 TOS 失败")
                    continue

                if registry:
                    filename = await registry.register(tos_url)
                    filenames.append(filename)
                    content_blocks.append({"type": "text", "text": f"生成了图片: @{filename}"})
                    content_blocks.append({"type": "text", "text": f"@{filename}:"})
                    content_blocks.append({"type": "image_url", "image_url": {"url": tos_url}})

            if not content_blocks:
                return "图片生成失败，请稍后重试"

            return content_blocks

    except Exception as e:
        logger.exception(f"Image agent执行失败: {str(e)}")
        return f"抱歉，处理您的请求时出现错误: {str(e)}"
