"""Worker 统一错误处理装饰器

cron_error_handler: arq cron job 的错误处理，log 后不中断调度器
mq_error_handler: MQ consumer 的错误处理，log 后 nack（不重入队列）
"""

import functools
import logging
from collections.abc import Callable
from typing import ParamSpec, TypeVar

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def cron_error_handler() -> Callable:
    """arq cron job 错误处理：log + 不中断调度器"""

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
            try:
                return await func(*args, **kwargs)  # type: ignore[misc]
            except Exception:
                logger.exception("Cron job %s failed", func.__name__)
                return None

        return wrapper  # type: ignore[return-value]

    return decorator


def mq_error_handler() -> Callable:
    """MQ consumer 错误处理：log + 尝试 nack（不重入队列）

    若 handler 内部已通过 message.process() 完成 ack/nack，
    二次 nack 会被安全忽略。
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(message, *args: P.args, **kwargs: P.kwargs) -> T | None:
            try:
                return await func(message, *args, **kwargs)  # type: ignore[misc]
            except Exception:
                logger.exception("MQ handler %s failed", func.__name__)
                if hasattr(message, "nack"):
                    try:
                        await message.nack(requeue=False)
                    except Exception:
                        pass  # 已被 message.process() 处理过
                return None

        return wrapper  # type: ignore[return-value]

    return decorator
