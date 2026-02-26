"""
时间范围解析工具（极简版）

只支持标准日期时间格式：
- YYYY-MM-DD（默认为当天 00:00）
- YYYY-MM-DD HH:mm

统一使用 Asia/Shanghai 时区
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz

# 服务器时区（Asia/Shanghai）
SERVER_TZ = pytz.timezone("Asia/Shanghai")


class TimeRangeParser:
    """时间范围解析器（极简版）"""

    @staticmethod
    def parse_datetime_str(dt_str: str) -> datetime:
        """
        解析日期时间字符串

        支持格式：
        - "2024-01-01" -> 2024-01-01 00:00:00
        - "2024-01-01 14:30" -> 2024-01-01 14:30:00

        返回：带时区的 datetime（Asia/Shanghai）
        """
        dt_str = dt_str.strip()

        # 尝试解析 "YYYY-MM-DD HH:mm" 或 "YYYY-MM-DD"
        for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d"]:
            try:
                dt = datetime.strptime(dt_str, fmt)
                return SERVER_TZ.localize(dt)
            except ValueError:
                continue

        raise ValueError(
            f"无效的日期时间格式: {dt_str}\n"
            f"支持格式: 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:mm'\n"
            f"示例: '2024-01-01' 或 '2024-01-01 14:30'"
        )

    @staticmethod
    def parse_time_input(
        time_input: str | None, default_to_now: bool = True
    ) -> datetime:
        """
        解析时间输入

        :param time_input: 时间字符串（日期时间/None）
        :param default_to_now: 为 None 时是否默认为当前时间
        :return: datetime 对象（带 Asia/Shanghai 时区）

        示例：
        - "2024-01-01" -> 2024-01-01 00:00:00
        - "2024-01-01 14:30" -> 2024-01-01 14:30:00
        - None -> 当前时间（如果 default_to_now=True）
        """
        if time_input is None:
            if default_to_now:
                return datetime.now(SERVER_TZ)
            raise ValueError("时间输入不能为空")

        return TimeRangeParser.parse_datetime_str(time_input)

    @staticmethod
    def to_milliseconds(dt: datetime) -> int:
        """转换为毫秒时间戳"""
        return int(dt.timestamp() * 1000)

    @staticmethod
    def from_milliseconds(ms: int) -> datetime:
        """从毫秒时间戳转换为 datetime（带 Asia/Shanghai 时区）"""
        dt = datetime.fromtimestamp(ms / 1000, tz=pytz.UTC)
        return dt.astimezone(SERVER_TZ)


class BackfillWindowGenerator:
    """回溯窗口生成器（按天切分，顺序处理）"""

    def __init__(
        self,
        start_time: datetime,
        end_time: datetime,
        max_messages_per_window: int = 300,
    ):
        """
        :param start_time: 开始时间
        :param end_time: 结束时间
        :param max_messages_per_window: 每窗口最大消息数
        """
        self.start_time = start_time
        self.end_time = end_time
        self.max_messages_per_window = max_messages_per_window

    def generate_windows(self) -> list[tuple[datetime, datetime]]:
        """
        生成时间窗口列表（按天切分）

        返回：[(window_start, window_end), ...]
        按时间顺序排列（从最早到最新）

        示例：
        start_time = 2024-01-01 00:00:00
        end_time = 2024-01-03 14:30:00
        生成窗口（按时间顺序）：
        1. [2024-01-01 00:00:00, 2024-01-02 00:00:00]  # 最早
        2. [2024-01-02 00:00:00, 2024-01-03 00:00:00]
        3. [2024-01-03 00:00:00, 2024-01-03 14:30:00]  # 最新
        """
        windows = []
        current_start = self.start_time

        while current_start < self.end_time:
            # 计算下一天的起始时间
            next_day_start = (current_start + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            # 窗口结束时间取 next_day_start 和 end_time 中较小的
            window_end = min(next_day_start, self.end_time)

            windows.append((current_start, window_end))
            current_start = window_end

        return windows

    def get_window_count(self) -> int:
        """获取窗口数量"""
        return len(self.generate_windows())


@dataclass
class BackfillWindow:
    """回溯窗口"""

    index: int
    start_time: datetime
    end_time: datetime


async def split_time(
    start_time: str | None = None,
    end_time: str | None = None,
    enable_backfill: bool = False,
) -> list[BackfillWindow]:
    """
    按照规则将时间范围切分为多个回溯窗口

    :param group_id: 群聊ID
    :param start_time: 开始时间（YYYY-MM-DD 或 YYYY-MM-DD HH:mm）
    :param end_time: 结束时间（可选，默认为当前时间）
    :param force: 是否强制更新
    :return: 回溯进度对象
    """
    # 解析时间范围
    start_dt = TimeRangeParser.parse_time_input(start_time, default_to_now=False)
    end_dt = TimeRangeParser.parse_time_input(end_time, default_to_now=True)

    if start_dt >= end_dt:
        raise ValueError(f"开始时间必须早于结束时间: start={start_dt}, end={end_dt}")

    if not enable_backfill:
        return [
            BackfillWindow(
                index=1,
                start_time=start_dt,
                end_time=end_dt,
            )
        ]

    # 生成时间窗口（按天切分）
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
