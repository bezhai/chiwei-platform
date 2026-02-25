"""Ark 客户端实现（火山引擎）"""

from typing import Any

from volcenginesdkarkruntime import AsyncArk
from volcenginesdkarkruntime.types.multimodal_embedding import (
    EmbeddingInputParam,
)

from app.agents.clients.base import BaseAIClient, HybridEmbedding, SparseVector


class ArkClient(BaseAIClient[AsyncArk]):
    """Ark客户端"""

    async def _create_client(self, model_info: dict) -> AsyncArk:
        """创建 AsyncArk 客户端实例。"""
        return AsyncArk(
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
        dimensions: int | None = 1024,
    ) -> list[float]:
        """多模态 embedding 能力实现。

        Args:
            text: 文本内容，可选
            image_base64_list: Base64 格式图片列表，可选
            instructions: 向量化指令（推荐使用 InstructionBuilder 构建）
            dimensions: 向量维度，默认 1024
        """
        client = self._ensure_connected()

        if not text and not image_base64_list:
            raise RuntimeError("ArkClient embed 需要至少提供 text 或一张图片")

        # 构造输入列表
        input_list: list[EmbeddingInputParam] = []

        # 添加文本
        if text:
            input_list.append({"type": "text", "text": text})

        # 添加图片
        if image_base64_list:
            for image_base64 in image_base64_list:
                input_list.append(
                    {"type": "image_url", "image_url": {"url": image_base64}}
                )

        # 调用 API
        resp = await client.multimodal_embeddings.create(
            model=self.model_name,
            input=input_list,
            dimensions=dimensions or 1024,
            encoding_format="float",
            extra_body={"instructions": instructions or ""},
        )

        return resp.data.embedding

    async def generate_image(
        self,
        prompt: str,
        size: str,
        reference_images: list[str] | None = None,
    ) -> list[str]:
        """Ark 图片生成能力，接口与 OpenAIClient 对齐。

        当前使用非流式、b64_json 的返回形式。
        """
        client = self._ensure_connected()

        resp = await client.images.generate(
            model=self.model_name,
            prompt=prompt,
            size=size,
            image=reference_images or None,
            response_format="b64_json",
            watermark=False,
            sequential_image_generation="disabled",
        )

        return [f"data:image/jpeg;base64,{image.b64_json}" for image in resp.data or []]

    async def embed_hybrid(
        self,
        text: str | None = None,
        image_base64_list: list[str] | None = None,
        instructions: str | None = None,
        dimensions: int = 1024,
    ) -> HybridEmbedding:
        """生成混合向量（Dense + Sparse）。

        对于包含图片的消息：
        - Dense 向量：多模态（文本+图片）
        - Sparse 向量：纯文本（需要额外请求）

        对于纯文本消息：
        - 一次请求同时获取 Dense 和 Sparse

        Args:
            text: 文本内容，可选
            image_base64_list: Base64 格式图片列表，可选
            instructions: 向量化指令
            dimensions: Dense 向量维度，默认 1024

        Returns:
            HybridEmbedding: 包含 dense 和 sparse 向量
        """
        client = self._ensure_connected()

        if not text and not image_base64_list:
            raise RuntimeError("embed_hybrid 需要至少提供 text 或一张图片")

        has_images = bool(image_base64_list)

        if not has_images and text:
            # 纯文本：一次请求同时获取 Dense 和 Sparse
            text_input: list[EmbeddingInputParam] = [{"type": "text", "text": text}]
            resp = await client.multimodal_embeddings.create(
                model=self.model_name,
                input=text_input,
                dimensions=dimensions,
                encoding_format="float",
                extra_body={
                    "instructions": instructions or "",
                    "sparse_embedding": {"type": "enabled"},
                },
            )
            dense_vector = resp.data.embedding
            sparse_data: Any = resp.data.sparse_embedding or []
        else:
            # 有图片：需要两次请求
            # 第一次：多模态获取 Dense
            dense_input: list[EmbeddingInputParam] = []
            if text:
                dense_input.append({"type": "text", "text": text})
            for image_base64 in image_base64_list or []:
                dense_input.append(
                    {"type": "image_url", "image_url": {"url": image_base64}}
                )

            dense_resp = await client.multimodal_embeddings.create(
                model=self.model_name,
                input=dense_input,
                dimensions=dimensions,
                encoding_format="float",
                extra_body={"instructions": instructions or ""},
            )
            dense_vector = dense_resp.data.embedding

            # 第二次：纯文本获取 Sparse（如果有文本）
            sparse_data = []
            if text:
                sparse_input: list[EmbeddingInputParam] = [
                    {"type": "text", "text": text}
                ]
                sparse_resp = await client.multimodal_embeddings.create(
                    model=self.model_name,
                    input=sparse_input,
                    dimensions=dimensions,
                    encoding_format="float",
                    extra_body={
                        "instructions": instructions or "",
                        "sparse_embedding": {"type": "enabled"},
                    },
                )
                sparse_data = sparse_resp.data.sparse_embedding or []

        # 转换 Sparse 格式：SparseEmbedding -> SparseVector
        # 火山引擎返回的 sparse_embedding 是 SparseEmbedding 对象，有 index 和 value 属性
        if sparse_data:
            # SparseEmbedding 对象是可迭代的，每个元素有 index 和 value 属性
            indices = [item.index for item in sparse_data]
            values = [item.value for item in sparse_data]
        else:
            indices = []
            values = []
        sparse_vector = SparseVector(indices=indices, values=values)

        return HybridEmbedding(dense=dense_vector, sparse=sparse_vector)
