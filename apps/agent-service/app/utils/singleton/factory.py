import asyncio
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

T = TypeVar("T")


class AsyncInitializable(ABC):
    """需要异步初始化的类需要实现此接口"""

    @abstractmethod
    async def async_init(self, *args, **kwargs) -> None:
        """异步初始化方法"""
        pass


class AsyncSingletonFactory(Generic[T]):
    _instances: dict[type[T], T] = {}
    _locks: dict[type[T], asyncio.Lock] = {}

    @classmethod
    async def get_instance(cls, target_class: type[T], *args, **kwargs) -> T:
        if target_class not in cls._instances:
            # 为每个类创建独立的锁
            if target_class not in cls._locks:
                cls._locks[target_class] = asyncio.Lock()

            async with cls._locks[target_class]:
                if target_class not in cls._instances:
                    # 创建实例
                    instance = target_class(*args, **kwargs)

                    # 如果实例实现了异步初始化接口，则调用异步初始化
                    if isinstance(instance, AsyncInitializable):
                        await instance.async_init(*args, **kwargs)

                    cls._instances[target_class] = instance

        return cls._instances[target_class]
