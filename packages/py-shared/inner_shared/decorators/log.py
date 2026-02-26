"""
Log decorator for function input/output logging.
Supports both sync and async functions.
"""

import functools
import inspect
import logging
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)


def log_io(func: F) -> F:
    """
    Universal decorator: logs function input/output, and errors on exception.
    Supports both sync and async functions.
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            logger.info(f"Calling {func.__name__} with args={args}, kwargs={kwargs}")
            try:
                result = await func(*args, **kwargs)
                logger.info(f"Called {func.__name__} returned: {result}")
                return result
            except Exception as e:
                logger.error(f"{func.__name__} raised exception: {repr(e)}, str(e): {str(e)}")
                raise

        return async_wrapper  # type: ignore
    else:

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            logger.info(f"Calling {func.__name__} with args={args}, kwargs={kwargs}")
            try:
                result = func(*args, **kwargs)
                logger.info(f"Called {func.__name__} returned: {result}")
                return result
            except Exception as e:
                logger.error(f"{func.__name__} raised exception: {repr(e)}, str(e): {str(e)}")
                raise

        return sync_wrapper  # type: ignore
