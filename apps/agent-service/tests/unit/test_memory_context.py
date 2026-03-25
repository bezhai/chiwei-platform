"""测试三层记忆上下文构建"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.memory_context import build_memory_context


@pytest.mark.asyncio
async def test_build_memory_context_group():
    """群聊场景：返回第一层 + 第二层（群感觉 + 人物 gestalt）"""
    with patch(
        "app.services.memory_context.build_inner_state",
        new_callable=AsyncMock,
        return_value="周三下午，有点犯困。今天没什么特别的安排。",
    ), patch(
        "app.services.memory_context.get_group_culture_gestalt",
        new_callable=AsyncMock,
        return_value="最放飞的群，二次元浓度拉满",
    ), patch(
        "app.services.memory_context.get_impressions_for_users",
        new_callable=AsyncMock,
        return_value=[
            MagicMock(user_id="u1", impression_text="群里的指挥官，嘴硬心软"),
        ],
    ), patch(
        "app.services.memory_context.get_username",
        new_callable=AsyncMock,
        return_value="A哥",
    ):
        result = await build_memory_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="A哥",
        )

    assert "犯困" in result
    assert "放飞" in result
    assert "指挥官" in result
    assert len(result) < 800


@pytest.mark.asyncio
async def test_build_memory_context_p2p():
    """私聊场景：应包含跨群印象"""
    with patch(
        "app.services.memory_context.build_inner_state",
        new_callable=AsyncMock,
        return_value="周末，心情不错。",
    ), patch(
        "app.services.memory_context.get_cross_group_impressions",
        new_callable=AsyncMock,
        return_value=[
            (MagicMock(impression_text="聊动画很带劲"), "KA群"),
        ],
    ):
        result = await build_memory_context(
            chat_id="p2p_001",
            chat_type="p2p",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="A哥",
        )

    assert "心情不错" in result
    assert "动画" in result
    assert "KA群" in result


@pytest.mark.asyncio
async def test_build_memory_context_empty_everything():
    """所有来源都为空时，仍返回内心状态"""
    with patch(
        "app.services.memory_context.build_inner_state",
        new_callable=AsyncMock,
        return_value="周一上午，精力还不错。",
    ), patch(
        "app.services.memory_context.get_group_culture_gestalt",
        new_callable=AsyncMock,
        return_value="",
    ), patch(
        "app.services.memory_context.get_impressions_for_users",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await build_memory_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=[],
            trigger_user_id="u1",
            trigger_username="A哥",
        )

    assert "精力还不错" in result
    # Should only have inner state, nothing else
    assert "感觉" not in result


@pytest.mark.asyncio
async def test_build_memory_context_no_diary_content():
    """不含日记全文（回归测试 — 防止旧行为回归）"""
    with patch(
        "app.services.memory_context.build_inner_state",
        new_callable=AsyncMock,
        return_value="下午。",
    ), patch(
        "app.services.memory_context.get_group_culture_gestalt",
        new_callable=AsyncMock,
        return_value="活跃的群",
    ), patch(
        "app.services.memory_context.get_impressions_for_users",
        new_callable=AsyncMock,
        return_value=[MagicMock(user_id="u1", impression_text="有趣的人")],
    ), patch(
        "app.services.memory_context.get_username",
        new_callable=AsyncMock,
        return_value="Test",
    ):
        result = await build_memory_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="Test",
        )

    # Must NOT contain diary date markers (old behavior)
    assert "--- 2026-" not in result
    assert "上周回顾" not in result
