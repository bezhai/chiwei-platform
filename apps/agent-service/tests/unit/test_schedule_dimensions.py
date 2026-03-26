# tests/unit/test_schedule_dimensions.py
import pytest
from datetime import date


def test_select_dimensions_always_includes_weather():
    """天气维度必选"""
    from app.workers.schedule_worker import _select_dimensions
    for _ in range(20):
        dims = _select_dimensions(date(2026, 3, 26))
        dim_names = [d["dim"] for d in dims]
        assert "weather" in dim_names


def test_select_dimensions_count():
    """选出 4-6 个维度"""
    from app.workers.schedule_worker import _select_dimensions
    for _ in range(20):
        dims = _select_dimensions(date(2026, 3, 26))
        assert 4 <= len(dims) <= 6


def test_select_dimensions_no_duplicates():
    """不重复选取"""
    from app.workers.schedule_worker import _select_dimensions
    for _ in range(20):
        dims = _select_dimensions(date(2026, 3, 26))
        dim_names = [d["dim"] for d in dims]
        assert len(dim_names) == len(set(dim_names))


def test_build_active_dimensions_text():
    """active_dimensions 文本生成"""
    from app.workers.schedule_worker import _build_active_dimensions_text
    dims = [
        {"dim": "weather", "label": "天气"},
        {"dim": "anime", "label": "二次元"},
        {"dim": "music", "label": "音乐"},
    ]
    text = _build_active_dimensions_text(dims)
    assert "天气" not in text  # weather is excluded from active_dimensions text
    assert "二次元" in text
    assert "音乐" in text
