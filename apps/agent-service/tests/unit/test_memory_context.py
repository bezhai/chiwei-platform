"""测试统一聊天注入上下文"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_build_inner_context_group():
    """群聊：场景 + 状态 + 群感觉 + 人物 + 引导语"""
    with (
        patch("app.services.memory_context.get_identity_state", new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="今天想出门逛逛"),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value="最放飞的群"),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[
            MagicMock(user_id="u1", impression_text="群里的指挥官", updated_at=None),
        ]),
        patch("app.services.memory_context.get_username", new_callable=AsyncMock, return_value="A哥"),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao", chat_name="KA技术群",
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
        patch("app.services.memory_context.get_identity_state", new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="心情不错"),
        patch("app.services.memory_context.get_cross_group_impressions", new_callable=AsyncMock, return_value=[
            (MagicMock(impression_text="聊动画很带劲"), "KA群"),
        ]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="p2p_001", chat_type="p2p", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao",
        )

    assert "私聊" in result
    assert "心情不错" in result
    assert "动画" in result
    assert "翻翻日记" in result


@pytest.mark.asyncio
async def test_build_inner_context_no_state():
    """无状态时仍包含场景和引导语"""
    with (
        patch("app.services.memory_context.get_identity_state", new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value=""),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value=""),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=[],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao", chat_name="测试群",
        )

    assert "群聊「测试群」" in result
    assert "今天的基调" not in result
    assert "翻翻日记" in result


@pytest.mark.asyncio
async def test_build_inner_context_no_diary_content():
    """不含日记全文（回归测试）"""
    with (
        patch("app.services.memory_context.get_identity_state", new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="今天下午"),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value="活跃"),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=[],
            trigger_user_id="u1", trigger_username="Test", persona_id="akao", chat_name="群",
        )

    assert "--- 2026-" not in result
    assert "上周回顾" not in result


@pytest.mark.asyncio
async def test_build_inner_context_injects_drift_state():
    """有漂移状态时，注入到 inner_context 且在今日基调之前"""
    with (
        patch("app.services.memory_context.get_identity_state", new_callable=AsyncMock, return_value="有点犯困但还不想睡，说话偏短偏懒"),
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="今天想出门逛逛"),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=[],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao", chat_name="测试群",
        )

    assert "有点犯困" in result
    assert "此刻的状态" in result
    # 漂移状态在今日基调之前
    assert result.index("此刻的状态") < result.index("今天的基调")


@pytest.mark.asyncio
async def test_build_inner_context_no_drift_fallback():
    """无漂移状态时正常 fallback"""
    with (
        patch("app.services.memory_context.get_identity_state", new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="精力充沛"),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=[],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao", chat_name="测试群",
        )

    assert "此刻的状态" not in result
    assert "精力充沛" in result


@pytest.mark.asyncio
async def test_build_people_gestalt_includes_updated_at():
    """印象注入时包含上次印象更新日期"""
    from datetime import datetime, timezone

    imp = MagicMock(
        user_id="u1", impression_text="很有趣的人",
        updated_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
    )

    with (
        patch("app.services.memory_context.get_impressions_for_users",
              new_callable=AsyncMock, return_value=[imp]),
        patch("app.services.memory_context.get_username",
              new_callable=AsyncMock, return_value="A哥"),
    ):
        from app.services.memory_context import _build_people_gestalt
        lines = await _build_people_gestalt("chat_001", ["u1"])

    assert len(lines) == 1
    assert "03月15日" in lines[0]
    assert "很有趣的人" in lines[0]
    assert "A哥" in lines[0]


@pytest.mark.asyncio
async def test_get_reply_style_fallback_to_base():
    """per-chat 无漂移 → fallback 到基线"""
    with (
        patch("app.services.memory_context.get_identity_state",
              new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context.get_base_reply_style",
              new_callable=AsyncMock, return_value="[感冒中] 说话短短的"),
    ):
        from app.services.memory_context import get_reply_style
        result = await get_reply_style("p2p_001", "akao")

    assert "感冒" in result


@pytest.mark.asyncio
async def test_get_reply_style_per_chat_takes_priority():
    """per-chat 有漂移 → 用 per-chat，不读基线"""
    with (
        patch("app.services.memory_context.get_identity_state",
              new_callable=AsyncMock, return_value="群里很嗨"),
        patch("app.services.memory_context.get_base_reply_style",
              new_callable=AsyncMock) as mock_base,
    ):
        from app.services.memory_context import get_reply_style
        result = await get_reply_style("chat_001", "akao")

    assert "很嗨" in result
    mock_base.assert_not_called()


@pytest.mark.asyncio
async def test_get_reply_style_fallback_to_default():
    """per-chat 无漂移 + 基线也无 → fallback 到调用方传入的 default_style"""
    with (
        patch("app.services.memory_context.get_identity_state",
              new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context.get_base_reply_style",
              new_callable=AsyncMock, return_value=None),
    ):
        from app.services.memory_context import get_reply_style
        result = await get_reply_style("p2p_001", "akao", default_style="来自persona的默认风格")

    assert "来自persona的默认风格" in result


@pytest.mark.asyncio
async def test_get_reply_style_uses_persona_id():
    """get_reply_style 应使用 persona_id 维度的 Redis key"""
    with (
        patch("app.services.memory_context.get_identity_state", return_value="drifted") as mock_drift,
        patch("app.services.memory_context.get_base_reply_style", return_value=None),
    ):
        from app.services.memory_context import get_reply_style
        result = await get_reply_style("chat_abc", "akao")
        mock_drift.assert_called_once_with("chat_abc", "akao")
        assert result == "drifted"
