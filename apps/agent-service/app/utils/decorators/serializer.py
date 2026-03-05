import functools
import logging
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
