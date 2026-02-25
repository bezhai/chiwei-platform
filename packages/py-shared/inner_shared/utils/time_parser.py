"""
Time range parsing utilities.

Supports standard datetime formats:
- YYYY-MM-DD (defaults to 00:00)
- YYYY-MM-DD HH:mm
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz

# Default timezone
DEFAULT_TZ = pytz.timezone("Asia/Shanghai")


class TimeRangeParser:
    """Time range parser."""

    def __init__(self, timezone: pytz.BaseTzInfo | None = None):
        """
        Initialize the parser.

        Args:
            timezone: Timezone to use (default: Asia/Shanghai)
        """
        self.tz = timezone or DEFAULT_TZ

    def parse_datetime_str(self, dt_str: str) -> datetime:
        """
        Parse a datetime string.

        Supported formats:
        - "2024-01-01" -> 2024-01-01 00:00:00
        - "2024-01-01 14:30" -> 2024-01-01 14:30:00

        Returns:
            Timezone-aware datetime
        """
        dt_str = dt_str.strip()

        for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d"]:
            try:
                dt = datetime.strptime(dt_str, fmt)
                return self.tz.localize(dt)
            except ValueError:
                continue

        raise ValueError(
            f"Invalid datetime format: {dt_str}\n"
            f"Supported formats: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:mm'\n"
            f"Examples: '2024-01-01' or '2024-01-01 14:30'"
        )

    def parse_time_input(self, time_input: str | None, default_to_now: bool = True) -> datetime:
        """
        Parse time input.

        Args:
            time_input: Time string (datetime/None)
            default_to_now: Whether to default to current time when None

        Returns:
            Timezone-aware datetime
        """
        if time_input is None:
            if default_to_now:
                return datetime.now(self.tz)
            raise ValueError("Time input cannot be empty")

        return self.parse_datetime_str(time_input)

    def to_milliseconds(self, dt: datetime) -> int:
        """Convert to milliseconds timestamp."""
        return int(dt.timestamp() * 1000)

    def from_milliseconds(self, ms: int) -> datetime:
        """Convert from milliseconds timestamp to timezone-aware datetime."""
        dt = datetime.fromtimestamp(ms / 1000, tz=pytz.UTC)
        return dt.astimezone(self.tz)


class BackfillWindowGenerator:
    """Backfill window generator (splits by day, processes in order)."""

    def __init__(
        self,
        start_time: datetime,
        end_time: datetime,
        max_messages_per_window: int = 300,
    ):
        """
        Args:
            start_time: Start time
            end_time: End time
            max_messages_per_window: Max messages per window
        """
        self.start_time = start_time
        self.end_time = end_time
        self.max_messages_per_window = max_messages_per_window

    def generate_windows(self) -> list[tuple[datetime, datetime]]:
        """
        Generate time window list (split by day).

        Returns:
            List of (window_start, window_end) tuples in chronological order
        """
        windows = []
        current_start = self.start_time

        while current_start < self.end_time:
            next_day_start = (current_start + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            window_end = min(next_day_start, self.end_time)
            windows.append((current_start, window_end))
            current_start = window_end

        return windows

    def get_window_count(self) -> int:
        """Get the number of windows."""
        return len(self.generate_windows())


@dataclass
class BackfillWindow:
    """Backfill window."""

    index: int
    start_time: datetime
    end_time: datetime


async def split_time(
    start_time: str | None = None,
    end_time: str | None = None,
    enable_backfill: bool = False,
    timezone: pytz.BaseTzInfo | None = None,
) -> list[BackfillWindow]:
    """
    Split time range into backfill windows.

    Args:
        start_time: Start time (YYYY-MM-DD or YYYY-MM-DD HH:mm)
        end_time: End time (optional, defaults to now)
        enable_backfill: Whether to enable backfill (split by day)
        timezone: Timezone to use

    Returns:
        List of BackfillWindow objects
    """
    parser = TimeRangeParser(timezone)

    start_dt = parser.parse_time_input(start_time, default_to_now=False)
    end_dt = parser.parse_time_input(end_time, default_to_now=True)

    if start_dt >= end_dt:
        raise ValueError(f"Start time must be before end time: start={start_dt}, end={end_dt}")

    if not enable_backfill:
        return [
            BackfillWindow(
                index=1,
                start_time=start_dt,
                end_time=end_dt,
            )
        ]

    generator = BackfillWindowGenerator(start_dt, end_dt)
    windows = generator.generate_windows()

    return [
        BackfillWindow(
            index=idx,
            start_time=window_start,
            end_time=window_end,
        )
        for idx, (window_start, window_end) in enumerate(windows, start=1)
    ]
