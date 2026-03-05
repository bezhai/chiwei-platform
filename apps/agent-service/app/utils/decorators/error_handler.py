"""
错误处理装饰器
"""

import functools
import logging
import traceback
from collections.abc import Callable
from typing import Any

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def handle_errors():
    """
    统一错误处理装饰器
    捕获所有异常并返回统一的错误响应格式
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                error_msg = str(e)
                stack_trace = traceback.format_exc()
                logger.error(f"接口调用失败: {error_msg}\n{stack_trace}")

                return JSONResponse(
                    status_code=500,
                    content={
                        "error": "Internal Server Error",
                        "message": error_msg,
                        "type": "api_error",
                    },
                )

        return wrapper

    return decorator
