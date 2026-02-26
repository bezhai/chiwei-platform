from redis.asyncio import ConnectionPool, Redis

from app.config import settings


class AsyncRedisClient:
    """
    异步 Redis 客户端单例封装
    """

    _instance = None

    @staticmethod
    def get_instance():
        """
        获取单例 Redis 客户端实例
        """
        if AsyncRedisClient._instance is None:
            pool = ConnectionPool(
                host=settings.redis_host,
                port="6379",
                password=settings.redis_password,
                decode_responses=True,
                max_connections=10,
            )
            AsyncRedisClient._instance = Redis(connection_pool=pool)
        return AsyncRedisClient._instance
