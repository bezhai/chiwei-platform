import functools
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def redis_cache(expire_seconds: int = 86400):  # 默认24小时(86400秒)
    """
    Redis缓存装饰器

    Args:
        expire_seconds: 缓存过期时间（秒），默认24小时
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # 生成缓存键
            cache_key = _generate_cache_key(func.__name__, args, kwargs)

            # 动态导入并获取Redis客户端
            from app.clients.redis import AsyncRedisClient

            redis_client = AsyncRedisClient.get_instance()

            try:
                # 尝试从缓存获取结果
                cached_result = await redis_client.get(cache_key)
                if cached_result:
                    logger.info(f"缓存命中: {func.__name__}, key: {cache_key}")
                    return json.loads(cached_result)

                # 缓存未命中，执行原函数
                logger.info(f"缓存未命中: {func.__name__}, key: {cache_key}")
                result = await func(*args, **kwargs)

                # 将结果存入缓存
                await redis_client.setex(
                    cache_key,
                    expire_seconds,
                    json.dumps(result, ensure_ascii=False, default=str),
                )
                logger.info(
                    f"结果已缓存: {func.__name__}, "
                    f"key: {cache_key}, expire: {expire_seconds}s"
                )

                return result

            except Exception as e:
                logger.error(f"缓存操作失败: {func.__name__}, error: {str(e)}")
                # 缓存操作失败时，直接执行原函数
                return await func(*args, **kwargs)

        return wrapper

    return decorator


def _generate_cache_key(func_name: str, args: tuple, kwargs: dict[str, Any]) -> str:
    """
    生成缓存键

    Args:
        func_name: 函数名
        args: 位置参数
        kwargs: 关键字参数

    Returns:
        缓存键字符串
    """
    # 构建缓存数据
    cache_data = {"func": func_name, "args": args, "kwargs": kwargs}

    # 序列化并生成哈希
    cache_str = json.dumps(cache_data, sort_keys=True, ensure_ascii=False, default=str)
    cache_hash = hashlib.md5(cache_str.encode("utf-8")).hexdigest()

    return f"bangumi_cache:{func_name}:{cache_hash}"
