"""客户端工厂"""

from typing import Any

from app.agents.clients.base import BaseAIClient, ClientType


async def create_client(model_id: str) -> BaseAIClient[Any]:
    """根据模型配置创建合适的客户端实例。

    Args:
        model_id: 内部模型 ID（alias 或 provider:model 形式）

    Returns:
        对应的 BaseAIClient 子类实例（如 OpenAIClient、ArkClient）
    """
    from app.agents.clients.ark_client import ArkClient
    from app.agents.clients.azure_http_client import AzureHttpClient
    from app.agents.clients.openai_client import OpenAIClient
    from app.agents.infra.model_builder import ModelBuilder

    model_info = await ModelBuilder._get_model_and_provider_info(model_id)
    if model_info is None or not model_info.get("is_active", True):
        raise ValueError(f"无法获取模型配置或模型未激活: {model_id}")

    client_type = (model_info.get("client_type") or ClientType.OPENAI).lower()

    if client_type == ClientType.OPENAI:
        return OpenAIClient(model_id)
    if client_type == ClientType.ARK:
        return ArkClient(model_id)
    if client_type == ClientType.AZURE_HTTP:
        return AzureHttpClient(model_id)

    raise ValueError(f"未知的 client_type: {client_type} (model_id={model_id})")
