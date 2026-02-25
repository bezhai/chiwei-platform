"""工具装饰器"""

import functools
import logging
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger(__name__)


def tool_error_handler(
    error_message: str = "操作失败",
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """统一的工具错误处理装饰器

    捕获工具执行中的异常，返回友好的错误消息。

    Args:
        error_message: 返回给用户的错误提示前缀

    Returns:
        装饰后的函数
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return await func(*args, **kwargs)  # type: ignore
            except Exception as e:
                logger.error(f"{func.__name__} failed: {e}", exc_info=True)
                return f"{error_message}: {str(e)}"  # type: ignore

        return wrapper  # type: ignore

    return decorator
