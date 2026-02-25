from uuid import UUID

from .crud import create_task, get_task_by_id
from .registry import TASK_REGISTRY


async def submit_task(
    task_type: str, initial_result: dict, max_retries: int = 3
) -> UUID:
    """
    提交新的长期任务

    Args:
        task_type: 任务类型标识符（必须已注册）
        initial_result: 初始参数字典
        max_retries: 最大重试次数

    Returns:
        UUID: 新创建的任务 ID

    Raises:
        KeyError: 如果 task_type 未注册
    """
    # 验证任务类型
    if task_type not in TASK_REGISTRY:
        raise KeyError(f"Task type '{task_type}' not registered")

    # 创建任务记录
    task_id = await create_task(
        task_type=task_type, initial_params=initial_result, max_retries=max_retries
    )

    return task_id


async def get_task_status(task_id: UUID) -> dict | None:
    """
    查询任务状态

    Args:
        task_id: 任务 ID

    Returns:
        任务信息字典，包含 status, current_result, error_log 等字段
        如果任务不存在返回 None
    """
    task = await get_task_by_id(task_id)

    if not task:
        return None

    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "current_result": task.current_result,
        "initial_params": task.initial_params,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "error_log": task.error_log,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
    }
