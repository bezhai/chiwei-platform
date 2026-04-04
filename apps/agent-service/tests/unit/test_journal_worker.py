# tests/unit/test_journal_worker.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date


@pytest.mark.asyncio
async def test_generate_daily_journal_basic():
    """基本场景：有日记、有昨天 journal、有今天 schedule"""
    diary1 = MagicMock(chat_id="chat1", diary_date="2026-03-25", content="今天在技术群聊了很多")
    diary2 = MagicMock(chat_id="chat2", diary_date="2026-03-25", content="和朋友私聊了追番的事")

    with (
        patch("app.workers.journal_worker.get_all_diaries_for_date", new_callable=AsyncMock, return_value=[diary1, diary2]),
        patch("app.workers.journal_worker.get_journal", new_callable=AsyncMock, return_value=None),
        patch("app.workers.journal_worker.get_plan_for_period", new_callable=AsyncMock, return_value=MagicMock(content="今天想出门走走")),
        patch("app.workers.journal_worker._get_recent_journals_text", new_callable=AsyncMock, return_value="--- 2026-03-25 ---\n昨天过得很平静"),
        patch("app.workers.journal_worker.get_prompt") as mock_prompt,
        patch("app.workers.journal_worker.ModelBuilder") as mock_mb,
        patch("app.workers.journal_worker.upsert_journal", new_callable=AsyncMock) as mock_upsert,
    ):
        mock_prompt.return_value.compile.return_value = "compiled prompt"
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(content="今天是个不错的一天")
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        from app.workers.journal_worker import generate_daily_journal
        result = await generate_daily_journal(date(2026, 3, 26), persona_id="akao")

    assert result == "今天是个不错的一天"
    mock_upsert.assert_awaited_once()
    # Verify upsert called with correct journal_type
    args, kwargs = mock_upsert.call_args
    assert args[0] == "daily"


@pytest.mark.asyncio
async def test_generate_daily_journal_skip_existing():
    """已存在时跳过"""
    existing = MagicMock(content="已有内容")
    with patch("app.workers.journal_worker.get_journal", new_callable=AsyncMock, return_value=existing):
        from app.workers.journal_worker import generate_daily_journal
        result = await generate_daily_journal(date(2026, 3, 26), persona_id="akao")

    assert result == "已有内容"


@pytest.mark.asyncio
async def test_generate_daily_journal_no_diaries():
    """没有日记时不生成 journal"""
    with (
        patch("app.workers.journal_worker.get_journal", new_callable=AsyncMock, return_value=None),
        patch("app.workers.journal_worker.get_all_diaries_for_date", new_callable=AsyncMock, return_value=[]),
    ):
        from app.workers.journal_worker import generate_daily_journal
        result = await generate_daily_journal(date(2026, 3, 26), persona_id="akao")

    assert result is None
