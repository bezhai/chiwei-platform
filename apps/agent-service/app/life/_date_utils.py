"""Shared date/time constants for the life engine modules."""

from datetime import timedelta, timezone

CST = timezone(timedelta(hours=8))

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_SEASON_MAP = {
    (3, 4, 5): "春天",
    (6, 7, 8): "夏天",
    (9, 10, 11): "秋天",
    (12, 1, 2): "冬天",
}


def get_season(month: int) -> str:
    for months, name in _SEASON_MAP.items():
        if month in months:
            return name
    return "未知"
