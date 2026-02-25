import functools
import inspect
import logging

logger = logging.getLogger(__name__)


def log_io(func):
    """
    通用装饰器：打印函数入参、出参，异常时记录错误信息。
    支持同步和异步函数。
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            logger.info(f"调用 {func.__name__} 入参: args={args}, kwargs={kwargs}")
            try:
                result = await func(*args, **kwargs)
                logger.info(f"调用 {func.__name__} 出参: {result}")
                return result
            except Exception as e:
                logger.error(f"{func.__name__} 执行异常: {repr(e)}，str(e): {str(e)}")
                raise

        return async_wrapper
    else:

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            logger.info(f"调用 {func.__name__} 入参: args={args}, kwargs={kwargs}")
            try:
                result = func(*args, **kwargs)
                logger.info(f"调用 {func.__name__} 出参: {result}")
                return result
            except Exception as e:
                logger.error(f"{func.__name__} 执行异常: {repr(e)}，str(e): {str(e)}")
                raise

        return sync_wrapper
