from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.future import select

from app.orm.base import AsyncSessionLocal

from .enums import TaskStatus
from .models import LongTask


async def create_task(
    task_type: str, initial_params: dict, max_retries: int = 3
) -> UUID:
    """
    创建新任务

    Args:
        task_type: 任务类型标识符
        initial_params: 初始参数字典
        max_retries: 最大重试次数

    Returns:
        UUID: 新创建的任务 ID
    """
    async with AsyncSessionLocal() as session:
        task = LongTask(
            task_type=task_type,
            status=TaskStatus.PENDING,
            current_result=initial_params,
            initial_params=initial_params,
            max_retries=max_retries,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def lock_tasks(
    executor_id: str, batch_size: int = 5, lock_timeout_seconds: int = 1800
) -> list[LongTask]:
    """
    批量锁定待执行的任务

    使用 CTE + FOR UPDATE SKIP LOCKED 实现原子性锁定

    Args:
        executor_id: 执行器唯一标识
        batch_size: 单次锁定的任务数量
        lock_timeout_seconds: 锁定超时时间（秒）

    Returns:
        已锁定的任务列表
    """
    async with AsyncSessionLocal() as session:
        lock_expiry = datetime.now() + timedelta(seconds=lock_timeout_seconds)

        # 使用 CTE 批量锁定任务
        query = text(
            """
            WITH selected AS (
                SELECT id FROM long_tasks
                WHERE status IN ('PENDING', 'COMMIT')
                  AND (locked_by IS NULL OR lock_expiry < NOW())
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            UPDATE long_tasks
            SET locked_by = :executor_id,
                lock_expiry = :lock_expiry,
                status = 'RUNNING',
                updated_at = NOW()
            FROM selected
            WHERE long_tasks.id = selected.id
            RETURNING long_tasks.*;
            """
        )

        result = await session.execute(
            query,
            {
                "batch_size": batch_size,
                "executor_id": executor_id,
                "lock_expiry": lock_expiry,
            },
        )

        await session.commit()

        # 将结果映射为 LongTask 对象
        rows = result.fetchall()
        tasks = []
        for row in rows:
            task = LongTask(
                id=row.id,
                task_type=row.task_type,
                status=row.status,
                current_result=row.current_result,
                initial_params=row.initial_params,
                created_at=row.created_at,
                updated_at=row.updated_at,
                locked_by=row.locked_by,
                lock_expiry=row.lock_expiry,
                error_log=row.error_log,
                retry_count=row.retry_count,
                max_retries=row.max_retries,
            )
            tasks.append(task)

        return tasks


async def update_task_success(task_id: UUID, new_result: dict, new_status: str) -> None:
    """
    更新任务执行成功后的状态

    Args:
        task_id: 任务 ID
        new_result: 新的结果字典
        new_status: 新的任务状态（COMMIT 或 DONE）
    """
    async with AsyncSessionLocal() as session:
        query = text(
            """
            UPDATE long_tasks
            SET current_result = :new_result,
                status = :new_status,
                locked_by = NULL,
                lock_expiry = NULL,
                updated_at = NOW()
            WHERE id = :task_id;
            """
        )

        await session.execute(
            query,
            {
                "task_id": task_id,
                "new_result": new_result,
                "new_status": new_status,
            },
        )

        await session.commit()


async def update_task_failure(
    task_id: UUID, error_log: str, should_retry: bool
) -> None:
    """
    更新任务执行失败后的状态

    Args:
        task_id: 任务 ID
        error_log: 错误日志（堆栈信息）
        should_retry: 是否应该重试
    """
    async with AsyncSessionLocal() as session:
        if should_retry:
            # 可重试：增加计数器，释放锁，保持 COMMIT 状态
            query = text(
                """
                UPDATE long_tasks
                SET retry_count = retry_count + 1,
                    error_log = :error_log,
                    status = 'COMMIT',
                    locked_by = NULL,
                    lock_expiry = NULL,
                    updated_at = NOW()
                WHERE id = :task_id;
                """
            )
        else:
            # 不可重试：标记为 FAILED
            query = text(
                """
                UPDATE long_tasks
                SET status = 'FAILED',
                    error_log = :error_log,
                    locked_by = NULL,
                    lock_expiry = NULL,
                    updated_at = NOW()
                WHERE id = :task_id;
                """
            )

        await session.execute(
            query,
            {
                "task_id": task_id,
                "error_log": error_log,
            },
        )

        await session.commit()


async def get_task_by_id(task_id: UUID) -> LongTask | None:
    """
    根据 ID 查询任务

    Args:
        task_id: 任务 ID

    Returns:
        任务对象，如果不存在返回 None
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(LongTask).where(LongTask.id == task_id))
        return result.scalar_one_or_none()
