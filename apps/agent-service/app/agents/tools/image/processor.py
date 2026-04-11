"""ImageProcessor — 统一图片上传+注册

将 "upload_to_tos → registry.register" 这一重复模式
从 search/image.py 和 image/generate.py 提取到此处。

注: context_builder.py 使用 process_image (Feishu 下载管线) + register_batch,
属于不同的数据流，不适合用此类替代。
"""

import logging

from app.clients.image_client import image_client
from app.clients.image_registry import ImageRegistry

logger = logging.getLogger(__name__)


class ImageProcessor:
    """Stateless helper for the upload-then-register pattern."""

    @staticmethod
    async def upload_and_register(
        source_type: str,
        data: str,
        registry: ImageRegistry | None = None,
    ) -> tuple[str, str | None]:
        """Upload to TOS, optionally register in ImageRegistry.

        Args:
            source_type: "url" or "base64" — passed to image_client.upload_to_tos.
            data: The URL or base64 payload.
            registry: If provided, the TOS URL is registered and the assigned
                      filename (e.g. '3.png') is returned as the second element.

        Returns:
            (tos_url, filename) on success.
            (original_data, None) on failure — callers can degrade gracefully.
        """
        try:
            tos_url = await image_client.upload_to_tos(source_type, data)
            if not tos_url:
                logger.warning("upload_to_tos returned None, falling back to original data")
                return data, None

            filename: str | None = None
            if registry:
                filename = await registry.register(tos_url)

            return tos_url, filename
        except Exception:
            logger.warning("upload_and_register failed, falling back to original data", exc_info=True)
            return data, None
