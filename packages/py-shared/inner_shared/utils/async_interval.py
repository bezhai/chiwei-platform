"""
Async interval checker utility.
"""

import asyncio


class AsyncIntervalChecker:
    """
    Async interval checker.
    Checks if a specified time interval has passed since the last trigger.
    """

    def __init__(self, interval_seconds: float):
        """
        Initialize the async interval checker.

        Args:
            interval_seconds: Interval in seconds
        """
        self.interval_seconds = interval_seconds
        self.last_trigger_time: float | None = None

    def check(self) -> bool:
        """
        Check if the interval has passed.

        Returns:
            True if interval has passed (and updates trigger time), False otherwise
        """
        current_time = asyncio.get_event_loop().time()

        # First call
        if self.last_trigger_time is None:
            self.last_trigger_time = current_time
            return True

        # Calculate time difference
        time_diff = current_time - self.last_trigger_time

        # If time difference >= interval
        if time_diff >= self.interval_seconds:
            self.last_trigger_time = current_time
            return True
        else:
            return False

    def reset(self) -> None:
        """Reset the trigger time."""
        self.last_trigger_time = None
