"""Format a Note's when_at / created_at into a human-readable Chinese label.

Used both by ``list_note`` tool output and ``build_active_notes_section``
context injection. Day boundaries are computed in CST (UTC+8) so "今天" /
"明天" align with the user's lived day, not UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_CST = timezone(timedelta(hours=8))


def _to_local_date(dt: datetime) -> datetime:
    return dt.astimezone(_CST).replace(hour=0, minute=0, second=0, microsecond=0)


def format_when_label(
    *,
    when_at: datetime | None,
    created_at: datetime,
    now: datetime,
) -> str:
    """Return a Chinese relative-time label.

    - ``when_at`` set: "今天" / "明天" / "还有 N 天" / "昨天就该做" / "已过期 N 天"
    - ``when_at`` None: "今天记的，没说时间" / "N 天前记的，没说时间"
    """
    today = _to_local_date(now)

    if when_at is not None:
        target = _to_local_date(when_at)
        delta_days = (target - today).days
        if delta_days == 0:
            return "今天"
        if delta_days == 1:
            return "明天"
        if delta_days >= 2:
            return f"还有 {delta_days} 天"
        if delta_days == -1:
            return "昨天就该做"
        return f"已过期 {-delta_days} 天"

    created_local = _to_local_date(created_at)
    delta_days = (today - created_local).days
    if delta_days <= 0:
        return "今天记的，没说时间"
    return f"{delta_days} 天前记的，没说时间"
