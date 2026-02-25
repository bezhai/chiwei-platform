from abc import ABC, abstractmethod


class BaseTask(ABC):
    """
    长期任务抽象基类

    子类必须实现 execute 方法来定义任务逻辑
    """

    def __init__(self, **result: dict):
        """
        初始化任务实例

        Args:
            **result: 上次执行的结果字典（首次执行时为 initial_params）
        """
        self.result = result

    @abstractmethod
    async def execute(self) -> tuple[dict, str]:
        """
        执行任务的单个步骤

        Returns:
            Tuple[Dict, str]: (新的结果字典, 新的任务状态)
                - 结果字典将保存到 current_result
                - 状态应为 TaskStatus 枚举值之一（COMMIT, DONE, FAILED）

        Raises:
            Exception: 任务执行失败时抛出异常，框架将自动处理重试逻辑
        """
        pass
