"""Model & Provider CRUD operations

Contains both pure query functions and business logic (parse/fallback).
"""

from sqlalchemy.future import select

from app.orm.base import AsyncSessionLocal
from app.orm.models import ModelMapping, ModelProvider


# ── Business logic (string parsing, not data access) ──


def parse_model_id(model_id: str) -> tuple[str, str]:
    """解析 model_id 格式："{供应商名称}:模型原名"

    Args:
        model_id: 格式为"供应商名称:模型原名"的字符串

    Returns:
        tuple: (供应商名称, 模型原名)
    """
    if ":" in model_id:
        provider_name, model_name = model_id.split(":", 1)
        return provider_name.strip(), model_name.strip()
    else:
        # 如果没有/，使用默认供应商302.ai
        return "302.ai", model_id.strip()


# ── Query + fallback logic ──


async def get_model_and_provider_info(model_id: str):
    """根据 model_id 获取供应商配置和模型名称

    优先查找 ModelMapping，如果未找到则回退到解析逻辑。

    Args:
        model_id: 映射别名 或 格式为"供应商名称/模型原名"的字符串

    Returns:
        dict: 包含模型和供应商信息的字典
    """
    async with AsyncSessionLocal() as session:
        # 1. 尝试查找映射
        mapping_result = await session.execute(
            select(ModelMapping).where(ModelMapping.alias == model_id)
        )
        mapping = mapping_result.scalar_one_or_none()

        if mapping:
            provider_name = mapping.provider_name
            actual_model_name = mapping.real_model_name
        else:
            # 2. 回退到解析逻辑
            provider_name, actual_model_name = parse_model_id(model_id)

        # 3. 查询供应商信息
        provider_result = await session.execute(
            select(ModelProvider).where(ModelProvider.name == provider_name)
        )
        provider = provider_result.scalar_one_or_none()

        # 如果找不到指定供应商，尝试使用默认的302.ai
        if not provider:
            provider_result = await session.execute(
                select(ModelProvider).where(ModelProvider.name == "302.ai")
            )
            provider = provider_result.scalar_one_or_none()

        if not provider:
            return None

        return {
            "model_name": actual_model_name,
            "api_key": provider.api_key,
            "base_url": provider.base_url,
            "is_active": provider.is_active,
            "client_type": provider.client_type or "openai",
            "use_proxy": provider.use_proxy,
        }
