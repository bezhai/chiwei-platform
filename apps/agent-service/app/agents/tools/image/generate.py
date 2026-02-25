"""图片生成工具"""

import asyncio
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
            description="一个明确的生成图片提示词。用自然语言清晰描述画面，写明 主体 + 行为 + 环境，可补充风格、色彩、光影、构图等美学元素。"
        ),
    ],
    size: Annotated[
        str,
        Field(description="图片尺寸。可选值：1K、2K、4K 或像素值如 2048x2048"),
    ] = "2048x2048",
    image_list: Annotated[
        list[int] | None,
        Field(description="参考图片列表，使用文本中的图片编号，从1开始"),
    ] = None,
) -> str | dict[str, Any]:
    """
    通过文本提示词生成图片, 返回图片image_key
    """
    try:
        context = get_runtime(AgentContext).context
        image_url_list = context.media.image_urls

        reference_urls = []
        if image_list and image_url_list:
            for idx in image_list:
                array_index = idx - 1
                if 0 <= array_index < len(image_url_list):
                    reference_urls.append(image_url_list[array_index])
                else:
                    logger.warning(
                        f"图片编号 {idx} 超出范围（总共 {len(image_url_list)} 张图片）"
                    )

        if reference_urls:
            logger.info(f"使用参考图片: {reference_urls}")

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

            image_keys = await batch_upload_images(base64_images=base64_images)
            return {
                "image_keys": image_keys,
            }

    except Exception as e:
        logger.exception(f"Image agent执行失败: {str(e)}")
        return f"抱歉，处理您的请求时出现错误: {str(e)}"


async def batch_upload_images(base64_images: list[str]) -> list[str]:
    """
    批量上传图片, 返回图片 keys 列表
    """
    from app.clients.image_client import image_client

    tasks = [
        image_client.upload_base64_image(base64_data=base64_image)
        for base64_image in base64_images
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    image_keys = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"图片上传失败: {str(result)}")
        elif isinstance(result, str) and result:
            image_keys.append(result)

    return image_keys
