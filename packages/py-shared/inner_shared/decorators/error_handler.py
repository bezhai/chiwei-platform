"""
Error handling decorator for FastAPI endpoints.
"""

import functools
import logging
import traceback
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def handle_errors():
    """
    Unified error handling decorator.
    Catches all exceptions and returns a unified error response format.
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
                logger.error(f"API call failed: {error_msg}\n{stack_trace}")

                # Import here to make fastapi optional
                try:
                    from fastapi.responses import JSONResponse
                    from sse_starlette.sse import EventSourceResponse

                    # Check if original function returns EventSourceResponse
                    if func.__annotations__.get("return") == EventSourceResponse:
                        return JSONResponse(
                            status_code=500,
                            content={
                                "error": "Internal Server Error",
                                "message": error_msg,
                                "type": "sse_error",
                            },
                        )

                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": "Internal Server Error",
                            "message": error_msg,
                            "type": "api_error",
                        },
                    )
                except ImportError:
                    # If FastAPI is not available, re-raise the exception
                    raise

        return wrapper

    return decorator
