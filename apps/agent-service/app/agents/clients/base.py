"""AI 客户端抽象基类"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, NamedTuple, TypeVar

logger = logging.getLogger(__name__)

ClientT = TypeVar("ClientT")


class SparseVector(NamedTuple):
    """稀疏向量结构"""

    indices: list[int]
    values: list[float]


@dataclass
class HybridEmbedding:
    """混合向量结构（Dense + Sparse）"""

    dense: list[float]
    sparse: SparseVector


class ClientType:
    """底层客户端类型枚举。

    主要通过 model_provider.client_type 进行配置：
    - "openai": OpenAI 兼容客户端
    - "ark": 火山引擎 Ark Runtime 客户端
    - "azure-http": 仅支持生图的 HTTP 客户端
    - "google": Google Generative AI 客户端
    """

    OPENAI = "openai"
    ARK = "ark"
    AZURE_HTTP = "azure-http"
    GOOGLE = "google"


class BaseAIClient(ABC, Generic[ClientT]):
    """AI客户端抽象基类，提供通用的生命周期管理。"""

    def __init__(self, model_id: str) -> None:
        self._client: ClientT | None = None
        self.model_id = model_id
        self.model_name: str = ""
        self.client_type: str | None = None

    @abstractmethod
    async def _create_client(self, model_info: dict) -> ClientT:
        """创建具体的客户端实例。

        Args:
            model_info: 包含 api_key, base_url, model 等信息的字典

        Returns:
            具体的客户端实例
        """
        ...

    async def connect(self) -> None:
        """初始化客户端连接。"""
        if self._client is None:
            from app.agents.infra.model_builder import ModelBuilder

            model_info = await ModelBuilder.get_basic_model_params(self.model_id)
            if model_info is None:
                raise ValueError(f"无法获取模型参数: {self.model_id}")
            self.model_name = model_info["model"]
            self.client_type = model_info.get("client_type")
            self._client = await self._create_client(model_info)

    async def disconnect(self) -> None:
        """关闭客户端连接。"""
        if self._client is not None:
            await self._client.close()  # type: ignore[union-attr]
            self._client = None

    def _ensure_connected(self) -> ClientT:
        """确保客户端已连接。

        Returns:
            已连接的客户端实例

        Raises:
            RuntimeError: 如果客户端未连接
        """
        if self._client is None:
            raise RuntimeError(
                f"{self.__class__.__name__} not connected. Call connect() first."
            )
        return self._client

    async def __aenter__(self):
        """异步上下文管理器入口。"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口。"""
        await self.disconnect()

    async def embed(
        self,
        text: str | None = None,
        image_base64_list: list[str] | None = None,
        instructions: str | None = None,
        dimensions: int | None = None,
    ) -> list[float]:
        """统一的 embedding 能力（默认不支持）。

        Args:
            text: 文本内容，可选
            image_base64_list: Base64 图片列表，可选
            instructions: 任务指令（如召回/聚类/STS 等），可选
            dimensions: 维度，具体是否生效由底层模型决定
        """
        raise RuntimeError(f"{self.__class__.__name__} 不支持 embed 能力")

    async def generate_image(
        self,
        prompt: str,
        size: str,
        reference_images: list[str] | None = None,
    ) -> list[str]:
        """文生图/图生图（默认不支持）。"""
        raise RuntimeError(f"{self.__class__.__name__} 不支持 generate_image 能力")

    async def embed_hybrid(
        self,
        text: str | None = None,
        image_base64_list: list[str] | None = None,
        instructions: str | None = None,
        dimensions: int = 1024,
    ) -> HybridEmbedding:
        """生成混合向量（Dense + Sparse）（默认不支持）。

        Args:
            text: 文本内容，可选
            image_base64_list: Base64 图片列表，可选
            instructions: 向量化指令
            dimensions: Dense 向量维度
        """
        raise RuntimeError(f"{self.__class__.__name__} 不支持 embed_hybrid 能力")
