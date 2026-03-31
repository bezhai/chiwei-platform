"""Gemini 原生画图客户端"""

import base64
import logging
from math import gcd

from google import genai
from google.genai import types

from app.agents.clients.base import BaseAIClient

logger = logging.getLogger(__name__)


class GeminiClient(BaseAIClient[genai.Client]):
    """Gemini 原生画图客户端。

    使用 google-genai SDK 调用 generateContent API，
    通过 response_modalities=["IMAGE"] 获取生成图片。
    只实现 generate_image 能力。
    """

    async def _create_client(self, model_info: dict) -> genai.Client:
        from app.config.config import settings

        http_opts: dict = {}
        if model_info.get("base_url"):
            http_opts["base_url"] = model_info["base_url"]
        if settings.forward_proxy_url:
            http_opts["client_args"] = {"proxy": settings.forward_proxy_url}

        return genai.Client(
            api_key=model_info["api_key"],
            http_options=types.HttpOptions(**http_opts) if http_opts else None,
        )

    async def disconnect(self) -> None:
        """genai.Client 无需显式关闭。"""
        self._client = None

    @staticmethod
    def _parse_size(size: str) -> tuple[str, str]:
        """从 size 参数解析宽高比和分辨率。

        返回: (aspect_ratio, image_size)

        支持:
        - "1K" / "2K" / "4K": 对应分辨率，宽高比默认 1:1
        - "WxH": 像素尺寸，自动计算最简比例和推断分辨率
        """
        size_str = size.strip().upper()

        if "X" in size_str:
            try:
                w_str, h_str = size_str.split("X", 1)
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
            except Exception:
                pass

        if size_str in {"1K", "2K", "4K"}:
            return "1:1", size_str

        return "1:1", "1K"

    async def generate_image(
        self,
        prompt: str,
        size: str,
        reference_images: list[str] | None = None,
    ) -> list[str]:
        """调用 Gemini generateContent API 生成图片。"""

        client = self._ensure_connected()
        aspect_ratio, image_size = self._parse_size(size)

        # 构造 contents
        contents: list[types.Part | str] = []

        if reference_images:
            for url in reference_images:
                contents.append(types.Part.from_uri(file_uri=url, mime_type="image/*"))

        contents.append(prompt)

        response = client.models.generate_content(
            model=self.model_name,
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
            raise RuntimeError("Gemini 生图接口未返回 candidates")

        images: list[str] = []
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                mime = part.inline_data.mime_type or "image/png"
                b64 = base64.b64encode(part.inline_data.data).decode()
                images.append(f"data:{mime};base64,{b64}")

        if not images:
            raise RuntimeError("Gemini 生图接口未在响应中找到图片数据")

        return images
