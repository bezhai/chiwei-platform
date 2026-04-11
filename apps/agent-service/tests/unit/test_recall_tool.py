"""测试 recall 工具"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_recall_fts_returns_fragments():
    """recall 用全文搜索返回匹配碎片"""
    frags = [
        MagicMock(
            content="和阿儒聊了新番的事",
            grain="conversation",
            created_at=MagicMock(strftime=MagicMock(return_value="04月05日")),
        ),
    ]
    with (
        patch(
            "app.agents.tools.recall.search_fragments_fts",
            new_callable=AsyncMock,
            return_value=frags,
        ),
        patch(
            "app.agents.tools.recall._get_persona_id",
            new_callable=AsyncMock,
            return_value="akao",
        ),
    ):
        from app.agents.tools.recall import _recall_impl

        result = await _recall_impl("新番")
    assert "新番" in result
    assert "04月05日" in result


@pytest.mark.asyncio
async def test_recall_empty_returns_hint():
    """无结果时返回自然语言提示"""
    with (
        patch(
            "app.agents.tools.recall.search_fragments_fts",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.agents.tools.recall._get_persona_id",
            new_callable=AsyncMock,
            return_value="akao",
        ),
    ):
        from app.agents.tools.recall import _recall_impl

        result = await _recall_impl("从来没聊过的话题")
    assert "想不起来" in result
