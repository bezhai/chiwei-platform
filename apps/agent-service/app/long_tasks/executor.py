import asyncio
import logging
import os
import socket
import traceback

from .crud import lock_tasks, update_task_failure, update_task_success
from .models import LongTask
from .registry import get_task_class

logger = logging.getLogger(__name__)


def get_executor_id() -> str:
    """生成执行器唯一标识: hostname:pid"""
    return f"{socket.gethostname()}:{os.getpid()}"


async def execute_single_task(task: LongTask) -> None:
    """
    执行单个任务

    Args:
        task: 已锁定的任务记录
    """
    try:
        # 1. 根据 task_type 获取任务类
        task_class = get_task_class(task.task_type)

        # 2. 实例化任务，传入 current_result
        task_instance = task_class(**task.current_result)

        # 3. 执行任务
        new_result, new_status = await task_instance.execute()

        # 4. 更新成功状态
        await update_task_success(
            task_id=task.id, new_result=new_result, new_status=new_status
        )

        logger.info(f"Task {task.id} executed successfully, new status: {new_status}")

    except Exception as e:
        # 5. 处理失败情况
        error_log = traceback.format_exc()
        should_retry = task.retry_count < task.max_retries

        await update_task_failure(
            task_id=task.id, error_log=error_log, should_retry=should_retry
        )

        logger.error(f"Task {task.id} failed: {str(e)}, retry={should_retry}")


async def poll_and_execute_tasks(
    batch_size: int = 5, lock_timeout_seconds: int = 1800
) -> None:
    """
    轮询并执行任务（arq 调用的主入口）

    Args:
        batch_size: 单次锁定的任务数量
        lock_timeout_seconds: 锁定超时时间（秒）
    """
    executor_id = get_executor_id()

    # 1. 批量锁定任务
    tasks = await lock_tasks(
        executor_id=executor_id,
        batch_size=batch_size,
        lock_timeout_seconds=lock_timeout_seconds,
    )

    if not tasks:
        logger.debug("No tasks to execute")
        return

    logger.info(f"Locked {len(tasks)} tasks for execution")

    # 2. 并发执行所有任务
    await asyncio.gather(
        *[execute_single_task(task) for task in tasks],
        return_exceptions=True,  # 确保单个任务失败不影响其他任务
    )
