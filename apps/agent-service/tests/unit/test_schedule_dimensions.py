# tests/unit/test_schedule_dimensions.py
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch


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


@pytest.mark.asyncio
async def test_generate_daily_plan_uses_journal():
    """daily plan 应使用 yesterday_journal 而非 recent_diary"""
    with (
        patch("app.workers.schedule_worker.get_plan_for_period", new_callable=AsyncMock, return_value=None),
        patch("app.workers.schedule_worker.get_journal", new_callable=AsyncMock, return_value=MagicMock(content="昨天过得很充实")),
        patch("app.workers.schedule_worker._gather_world_context", new_callable=AsyncMock, return_value=("世界素材", "今天可能涉及：音乐、美食")),
        patch("app.workers.schedule_worker.get_prompt") as mock_prompt,
        patch("app.workers.schedule_worker.ModelBuilder") as mock_mb,
        patch("app.workers.schedule_worker.upsert_schedule", new_callable=AsyncMock),
    ):
        mock_prompt.return_value.compile.return_value = "compiled"
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(content="今日手帐内容")
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(date(2026, 3, 27))

    # 验证 prompt compile 时传入了 yesterday_journal 和 active_dimensions
    compile_kwargs = mock_prompt.return_value.compile.call_args[1]
    assert "yesterday_journal" in compile_kwargs
    assert "active_dimensions" in compile_kwargs
    assert compile_kwargs["yesterday_journal"] == "昨天过得很充实"
    # 验证旧变量不再存在
    assert "recent_diary" not in compile_kwargs
    assert "yesterday_plan" not in compile_kwargs
