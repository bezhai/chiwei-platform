from .base import BaseTask

# 全局任务注册表
TASK_REGISTRY: dict[str, type[BaseTask]] = {}


def task_register(task_type: str):
    """
    任务类注册装饰器

    Args:
        task_type: 唯一的任务类型标识符

    Usage:
        @task_register("my_task")
        class MyTask(BaseTask):
            async def execute(self):
                ...
    """

    def decorator(cls: type[BaseTask]):
        if not issubclass(cls, BaseTask):
            raise TypeError(f"{cls.__name__} must inherit from BaseTask")

        if task_type in TASK_REGISTRY:
            raise ValueError(f"Task type '{task_type}' already registered")

        TASK_REGISTRY[task_type] = cls
        return cls

    return decorator


def get_task_class(task_type: str) -> type[BaseTask]:
    """
    根据 task_type 获取任务类

    Args:
        task_type: 任务类型标识符

    Returns:
        任务类

    Raises:
        KeyError: 如果任务类型未注册
    """
    if task_type not in TASK_REGISTRY:
        raise KeyError(f"Task type '{task_type}' not registered")
    return TASK_REGISTRY[task_type]
