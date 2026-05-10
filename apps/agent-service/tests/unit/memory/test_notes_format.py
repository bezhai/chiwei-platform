"""Test format_when_label helper for Notes context injection / list_note tool."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.memory.notes_format import format_when_label


_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


def _at(days: int, hour: int = 12) -> datetime:
    return _NOW + timedelta(days=days, hours=hour - 12)


@pytest.mark.parametrize(
    ("when_at", "created_at", "expected"),
    [
        # when_at present
        (_at(0), _at(0), "今天"),
        (_at(1), _at(0), "明天"),
        (_at(2), _at(0), "还有 2 天"),
        (_at(7), _at(0), "还有 7 天"),
        (_at(-1), _at(0), "昨天就该做"),
        (_at(-2), _at(0), "已过期 2 天"),
        (_at(-5), _at(0), "已过期 5 天"),
        # when_at None
        (None, _NOW, "今天记的，没说时间"),
        (None, _NOW - timedelta(days=1), "1 天前记的，没说时间"),
        (None, _NOW - timedelta(days=14), "14 天前记的，没说时间"),
    ],
)
def test_format_when_label(when_at, created_at, expected):
    assert format_when_label(when_at=when_at, created_at=created_at, now=_NOW) == expected
