"""OpenAI 客户端实现"""

from typing import Any

from openai import AsyncOpenAI

from app.agents.clients.base import BaseAIClient


class OpenAIClient(BaseAIClient[AsyncOpenAI]):
    """Async OpenAI API client wrapper."""

    async def _create_client(self, model_info: dict) -> AsyncOpenAI:
        """创建 AsyncOpenAI 客户端实例。"""
        return AsyncOpenAI(
            api_key=model_info["api_key"],
            base_url=model_info["base_url"],
            timeout=60.0,
            max_retries=3,
        )

    async def embed(
        self,
        text: str | None = None,
        image_base64_list: list[str] | None = None,
        instructions: str | None = None,
        dimensions: int | None = None,
    ) -> list[float]:
        """统一的 embedding 能力实现（当前仅支持纯文本）。"""

        # OpenAI embeddings 当前不支持多模态图片输入，如果调用方传了图片，直接抛错
        if image_base64_list:
            raise RuntimeError(
                "OpenAIClient 当前不支持带图片的多模态 embedding，请仅传入 text"
            )

        if not text:
            raise RuntimeError("OpenAIClient embed 需要提供 text 内容")

        client = self._ensure_connected()
        resp = await client.embeddings.create(model=self.model_name, input=text)
        return list(resp.data[0].embedding)

    async def generate_image(
        self,
        prompt: str,
        size: str,
        reference_images: list[str] | None = None,
    ) -> list[str]:
        """统一的图片生成能力实现（OpenAI 兼容接口）。"""
        client = self._ensure_connected()
        extra_body: dict[str, Any] = {
            "watermark": False,
            "sequential_image_generation": "disabled",
        }

        # 如果提供了参考图片，添加到 extra_body
        if reference_images:
            extra_body["image"] = reference_images

        resp = await client.images.generate(
            model=self.model_name,
            response_format="b64_json",
            prompt=prompt,
            size=size,  # pyright: ignore[reportArgumentType]
            n=1,
            extra_body=extra_body,
        )
        return [f"data:image/jpeg;base64,{image.b64_json}" for image in resp.data or []]
