"""测试 load_memory 工具的各种回忆模式"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.tools.memory import (
    _recall_recent,
    _recall_person,
    _recall_diary,
    _recall_topic,
)


@pytest.mark.asyncio
async def test_recall_recent():
    """recent 模式返回 Journal daily 摘要"""
    journals = [
        MagicMock(journal_date="2026-03-25", content="今天心情不错，出门逛了很久，" * 30),
        MagicMock(journal_date="2026-03-24", content="昨天有点累"),
    ]
    with patch("app.agents.tools.memory.get_recent_journals", new_callable=AsyncMock, return_value=journals):
        result = await _recall_recent("3")

    assert "2026-03-24" in result
    assert "2026-03-25" in result
    assert "..." in result  # 长内容被截断


@pytest.mark.asyncio
async def test_recall_recent_empty():
    """recent 模式无数据"""
    with patch("app.agents.tools.memory.get_recent_journals", new_callable=AsyncMock, return_value=[]):
        result = await _recall_recent("3")

    assert "没什么特别" in result


@pytest.mark.asyncio
async def test_recall_person():
    """person 模式返回印象"""
    with (
        patch("app.agents.tools.memory.search_user_by_name", new_callable=AsyncMock, return_value=[MagicMock(union_id="u1")]),
        patch("app.agents.tools.memory.get_impressions_for_users", new_callable=AsyncMock, return_value=[
            MagicMock(user_id="u1", impression_text="很有趣的人"),
        ]),
        patch("app.agents.tools.memory.get_username", new_callable=AsyncMock, return_value="陈儒"),
    ):
        result = await _recall_person("chat1", "陈儒")

    assert "陈儒" in result
    assert "有趣" in result


@pytest.mark.asyncio
async def test_recall_diary_truncated():
    """diary 模式返回摘要而非全文"""
    long_content = "A" * 500
    diary = MagicMock(diary_date="2026-03-20", content=long_content)
    with patch("app.agents.tools.memory.get_diary_by_date", new_callable=AsyncMock, return_value=diary):
        result = await _recall_diary("chat1", "2026-03-20")

    assert "2026-03-20" in result
    assert "..." in result
    assert len(result) < 500  # 被截断了


@pytest.mark.asyncio
async def test_recall_topic():
    """topic 模式按关键词搜索"""
    entries = [
        MagicMock(diary_date="2026-03-18", content="今天聊了新番推荐的事"),
    ]
    with patch("app.agents.tools.memory.search_diary_by_keyword", new_callable=AsyncMock, return_value=entries):
        result = await _recall_topic("新番")

    assert "2026-03-18" in result
    assert "新番" in result


@pytest.mark.asyncio
async def test_recall_topic_not_found():
    """topic 模式无结果"""
    with patch("app.agents.tools.memory.search_diary_by_keyword", new_callable=AsyncMock, return_value=[]):
        result = await _recall_topic("火锅")

    assert "想不起来" in result
    assert "火锅" in result
