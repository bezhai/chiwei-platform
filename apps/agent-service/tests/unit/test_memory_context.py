"""测试统一聊天注入上下文"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_build_inner_context_group():
    """群聊：场景 + 状态 + 群感觉 + 人物 + 引导语"""
    with (
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="今天想出门逛逛"),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value="最放飞的群"),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[
            MagicMock(user_id="u1", impression_text="群里的指挥官"),
        ]),
        patch("app.services.memory_context.get_username", new_callable=AsyncMock, return_value="A哥"),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥", chat_name="KA技术群",
        )

    assert "群聊「KA技术群」" in result
    assert "回复 A哥" in result
    assert "想出门逛逛" in result
    assert "放飞" in result
    assert "指挥官" in result
    assert "翻翻日记" in result


@pytest.mark.asyncio
async def test_build_inner_context_p2p():
    """私聊：场景 + 状态 + 跨群印象 + 引导语"""
    with (
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="心情不错"),
        patch("app.services.memory_context.get_cross_group_impressions", new_callable=AsyncMock, return_value=[
            (MagicMock(impression_text="聊动画很带劲"), "KA群"),
        ]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="p2p_001", chat_type="p2p", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥",
        )

    assert "私聊" in result
    assert "心情不错" in result
    assert "动画" in result
    assert "翻翻日记" in result


@pytest.mark.asyncio
async def test_build_inner_context_no_state():
    """无状态时仍包含场景和引导语"""
    with (
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value=""),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value=""),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=[],
            trigger_user_id="u1", trigger_username="A哥", chat_name="测试群",
        )

    assert "群聊「测试群」" in result
    assert "今天的状态" not in result
    assert "翻翻日记" in result


@pytest.mark.asyncio
async def test_build_inner_context_no_diary_content():
    """不含日记全文（回归测试）"""
    with (
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="今天下午"),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value="活跃"),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=[],
            trigger_user_id="u1", trigger_username="Test", chat_name="群",
        )

    assert "--- 2026-" not in result
    assert "上周回顾" not in result
