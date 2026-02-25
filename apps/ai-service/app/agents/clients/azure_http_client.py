"""Azure HTTP 客户端实现（仅支持生图）"""

import asyncio
from math import gcd
from typing import Any

import requests

from app.agents.clients.base import BaseAIClient


class AzureHttpClient(BaseAIClient[requests.Session]):
    """仅支持生图的 Azure HTTP 客户端。

    适配内部多模态生图接口（用法同 gptv），通过 HTTP 调用：
    - 使用 ModelProvider.base_url 作为完整的请求 URL
    - 使用 ModelProvider.model_name 作为请求体中的 model 字段
    - 只实现 generate_image 能力，embed 等其它能力一律不支持
    """

    def __init__(self, model_id: str) -> None:
        super().__init__(model_id)
        self._endpoint: str | None = None
        # HTTP 接口使用 ak 作为查询参数做鉴权
        self._api_key: str | None = None

    async def _create_client(self, model_info: dict) -> requests.Session:
        # 对于 HTTP 客户端，base_url 约定为完整的调用 URL
        self._endpoint = model_info["base_url"]
        # 与官方示例保持一致，使用 ak 查询参数做鉴权
        self._api_key = model_info.get("api_key")
        return requests.Session()

    async def disconnect(self) -> None:
        """关闭 HTTP 客户端会话。

        requests.Session.close 是同步方法，不能直接被 await。
        这里覆盖基类的实现，避免对同步 close 结果执行 await。
        """
        if self._client is not None:
            # requests.Session.close() 为同步方法，直接调用即可
            self._client.close()  # type: ignore[union-attr]
            self._client = None

    @staticmethod
    def _build_image_config(size: str) -> dict[str, Any]:
        """根据 size 构造 image_config。

        支持两类入参：
        - "1K" / "2K" / "4K"：直接映射到 imageSize，aspectRatio 默认为 1:1
        - "WxH"：像素尺寸，自动计算宽高比，并按最长边粗略映射到 1K/2K/4K
        """

        size_str = size.strip().upper()
        aspect_ratio = "1:1"
        image_size = "1K"

        if "X" in size_str:
            try:
                w_str, h_str = size_str.split("X", 1)
                w = int(w_str)
                h = int(h_str)
                if w > 0 and h > 0:
                    # 计算最简比例
                    g = gcd(w, h)
                    aspect_ratio = f"{w // g}:{h // g}"

                    longest = max(w, h)
                    if longest <= 1440:
                        image_size = "1K"
                    elif longest <= 2048:
                        image_size = "2K"
                    else:
                        image_size = "4K"
            except Exception:
                # 解析失败时退回默认配置
                aspect_ratio = "1:1"
                image_size = "1K"
        elif size_str in {"1K", "2K", "4K"}:
            image_size = size_str

        return {
            "aspectRatio": aspect_ratio,
            "imageSize": image_size,
            "imageOutputOptions": {"mimeType": "image/png"},
        }

    async def generate_image(
        self,
        prompt: str,
        size: str,
        reference_images: list[str] | None = None,
        *,
        n: int = 1,
    ) -> list[str]:
        """调用 HTTP 多模态生图接口。

        当前实现：
        - 仅支持生成单张图片（忽略 n>1 的情况）
        - 支持携带参考图片 URL
        """

        if not self._endpoint:
            raise RuntimeError("AzureHttpClient 未正确初始化 endpoint")

        session = self._ensure_connected()

        # 构造 messages：文本 + 可选参考图（用 image_url.url）
        contents: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
        ]

        if reference_images:
            for url in reference_images:
                contents.append({"type": "image_url", "image_url": {"url": url}})

        payload: dict[str, Any] = {
            "stream": False,
            "model": self.model_name,
            "max_tokens": 5000,
            "messages": [
                {
                    "role": "user",
                    "content": contents,
                }
            ],
            "response_modalities": ["TEXT", "IMAGE"],
            "image_config": self._build_image_config(size),
        }

        def _do_request() -> dict[str, Any]:
            headers = {
                "Content-Type": "application/json",
            }
            params: dict[str, Any] | None = None
            if self._api_key:
                # 官方接口通过 ak 查询参数做鉴权
                params = {"ak": self._api_key}

            resp = session.post(
                self._endpoint or "",
                params=params,
                json=payload,
                headers=headers,
                timeout=240,
            )
            resp.raise_for_status()
            return resp.json()

        data = await asyncio.to_thread(_do_request)

        # 解析返回的多模态内容，只提取图片
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("HTTP 生图接口未返回 choices")

        message = choices[0].get("message") or {}
        multimodal_contents = message.get("multimodal_contents") or []

        images: list[str] = []
        for item in multimodal_contents:
            if item.get("type") != "inline_data":
                continue
            inline_data = item.get("inline_data") or {}
            mime_type = inline_data.get("mime_type") or "image/png"
            b64_data = inline_data.get("data")
            if b64_data:
                images.append(f"data:{mime_type};base64,{b64_data}")

        if not images:
            raise RuntimeError("HTTP 生图接口未在响应中找到图片数据")

        # 目前接口实际上只返回单图，按统一约定返回列表
        return images
