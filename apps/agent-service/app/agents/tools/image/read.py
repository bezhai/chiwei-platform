"""图片查看工具"""

import logging
from typing import Annotated, Any

from langchain.tools import tool
from langgraph.runtime import get_runtime
from pydantic import Field

from app.agents.core.context import AgentContext
from app.agents.tools.decorators import tool_error_handler

logger = logging.getLogger(__name__)


@tool
@tool_error_handler(error_message="查看图片失败")
async def read_images(
    filenames: Annotated[
        list[str],
        Field(description="要查看的图片文件名列表，如 [\"3.png\", \"5.png\"]"),
    ],
) -> str | list[dict[str, Any]]:
    """查看指定的图片内容。对话中提到但未直接展示的图片，可以用此工具查看。

    Args:
        filenames: 图片文件名列表（如 ["3.png", "5.png"]）
    """
    context = get_runtime(AgentContext).context
    registry = context.media.registry

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
        logger.warning(f"图片未找到: {not_found}")

    if not content_blocks:
        return f"未找到图片: {', '.join(not_found)}"

    if not_found:
        content_blocks.insert(
            0, {"type": "text", "text": f"未找到: {', '.join(not_found)}"}
        )

    return content_blocks
