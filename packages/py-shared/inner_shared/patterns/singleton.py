"""
Async singleton factory pattern.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

T = TypeVar("T")


class AsyncInitializable(ABC):
    """Interface for classes that need async initialization."""

    @abstractmethod
    async def async_init(self, *args, **kwargs) -> None:
        """Async initialization method."""
        pass


class AsyncSingletonFactory(Generic[T]):
    """
    Async singleton factory.
    Creates and manages singleton instances with async initialization support.
    """

    _instances: dict[type[T], T] = {}
    _locks: dict[type[T], asyncio.Lock] = {}

    @classmethod
    async def get_instance(cls, target_class: type[T], *args, **kwargs) -> T:
        """
        Get or create a singleton instance of the target class.

        Args:
            target_class: The class to instantiate
            *args: Positional arguments for the constructor
            **kwargs: Keyword arguments for the constructor

        Returns:
            The singleton instance
        """
        if target_class not in cls._instances:
            # Create a lock for each class
            if target_class not in cls._locks:
                cls._locks[target_class] = asyncio.Lock()

            async with cls._locks[target_class]:
                if target_class not in cls._instances:
                    # Create instance
                    instance = target_class(*args, **kwargs)

                    # Call async init if implemented
                    if isinstance(instance, AsyncInitializable):
                        await instance.async_init(*args, **kwargs)

                    cls._instances[target_class] = instance

        return cls._instances[target_class]

    @classmethod
    def reset(cls, target_class: type[T] | None = None) -> None:
        """
        Reset singleton instance(s).

        Args:
            target_class: Specific class to reset, or None to reset all
        """
        if target_class is not None:
            cls._instances.pop(target_class, None)
            cls._locks.pop(target_class, None)
        else:
            cls._instances.clear()
            cls._locks.clear()
