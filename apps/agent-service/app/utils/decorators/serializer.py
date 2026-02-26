import functools
import json
import logging
from collections.abc import AsyncGenerator
from inspect import iscoroutinefunction

from pydantic import BaseModel

logger = logging.getLogger(__name__)


def dict_serialize(func):
    """装饰器：自动序列化 Pydantic 模型返回值，支持同步和异步函数"""

    if iscoroutinefunction(func):
        # 异步函数
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            return _serialize_dict(result)

        return async_wrapper
    else:
        # 同步函数
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            return _serialize_dict(result)

        return sync_wrapper


def _serialize_dict(data):
    """递归序列化字典中的 Pydantic 模型"""
    if isinstance(data, BaseModel):
        return data.model_dump()
    elif isinstance(data, list):
        return [
            item.model_dump() if isinstance(item, BaseModel) else item for item in data
        ]
    elif isinstance(data, dict):
        return {key: _serialize_dict(value) for key, value in data.items()}
    return data


def auto_json_serialize(func):
    """
    自动序列化装饰器
    将异步生成器中的 Pydantic 模型自动转换为 JSON 字符串
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs) -> AsyncGenerator[str, None]:
        async for item in func(*args, **kwargs):
            try:
                # 检查是否是 Pydantic 模型
                if hasattr(item, "model_dump_json"):
                    yield item.model_dump_json()
                elif hasattr(item, "json"):  # 兼容 Pydantic v1
                    yield item.json()
                elif isinstance(item, dict):
                    yield json.dumps(item, ensure_ascii=False)
                else:
                    # 其他类型直接返回字符串
                    yield str(item)
            except Exception as e:
                logger.error(f"序列化失败: {e}, item: {item}")
                # 兜底处理
                yield str(item)

    return wrapper
