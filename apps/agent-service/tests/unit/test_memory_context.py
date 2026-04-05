"""测试统一聊天注入上下文 v3（experience_fragment 版）"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_fragment(content: str, grain: str = "conversation", source_chat_id: str = "chat_001"):
    """创建模拟碎片"""
    return MagicMock(content=content, grain=grain, source_chat_id=source_chat_id)


# ── build_inner_context tests ──


@pytest.mark.asyncio
async def test_build_inner_context_group_with_fragments():
    """群聊：场景 + schedule + 当前群碎片 + daily + 引导语"""
    today_frags = [
        _make_fragment("和A哥聊了动画", source_chat_id="chat_001"),
        _make_fragment("有人分享了新番", grain="glimpse", source_chat_id="chat_001"),
        _make_fragment("私聊里B说了秘密", source_chat_id="p2p_999"),  # 应被过滤
    ]
    daily_frags = [
        _make_fragment("昨天去了咖啡店", grain="daily", source_chat_id=""),
    ]

    with (
        patch("app.services.memory_context._build_today_state",
              new_callable=AsyncMock, return_value="今天想出门逛逛"),
        patch("app.services.memory_context.get_today_fragments",
              new_callable=AsyncMock, return_value=today_frags),
        patch("app.services.memory_context.get_recent_fragments_by_grain",
              new_callable=AsyncMock, return_value=daily_frags),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao",
            chat_name="KA技术群",
        )

    assert "群聊「KA技术群」" in result
    assert "回复 A哥" in result
    assert "想出门逛逛" in result
    assert "聊了动画" in result
    assert "新番" in result
    assert "秘密" not in result  # p2p 碎片被过滤
    assert "咖啡店" in result  # daily 碎片
    assert "recall" in result


@pytest.mark.asyncio
async def test_build_inner_context_p2p_sees_all():
    """私聊：可以看到所有碎片（包括其他群和 p2p）"""
    today_frags = [
        _make_fragment("群里的讨论", source_chat_id="chat_001"),
        _make_fragment("另一个群的八卦", source_chat_id="chat_002"),
        _make_fragment("私聊说的心事", source_chat_id="p2p_001"),
    ]

    with (
        patch("app.services.memory_context._build_today_state",
              new_callable=AsyncMock, return_value="心情不错"),
        patch("app.services.memory_context.get_today_fragments",
              new_callable=AsyncMock, return_value=today_frags),
        patch("app.services.memory_context.get_recent_fragments_by_grain",
              new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="p2p_001", chat_type="p2p", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao",
        )

    assert "私聊" in result
    assert "群里的讨论" in result
    assert "八卦" in result
    assert "心事" in result


@pytest.mark.asyncio
async def test_build_inner_context_group_filters_private():
    """群聊：过滤掉 p2p 和其他群的碎片"""
    today_frags = [
        _make_fragment("当前群的话题", source_chat_id="chat_001"),
        _make_fragment("私聊的秘密", source_chat_id="p2p_001"),
        _make_fragment("其他群的讨论", source_chat_id="chat_999"),
    ]

    with (
        patch("app.services.memory_context._build_today_state",
              new_callable=AsyncMock, return_value=""),
        patch("app.services.memory_context.get_today_fragments",
              new_callable=AsyncMock, return_value=today_frags),
        patch("app.services.memory_context.get_recent_fragments_by_grain",
              new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao",
            chat_name="测试群",
        )

    assert "当前群的话题" in result
    assert "秘密" not in result
    assert "其他群" not in result


@pytest.mark.asyncio
async def test_build_inner_context_no_fragments():
    """无碎片时：场景 + 引导语，不出现空段"""
    with (
        patch("app.services.memory_context._build_today_state",
              new_callable=AsyncMock, return_value=""),
        patch("app.services.memory_context.get_today_fragments",
              new_callable=AsyncMock, return_value=[]),
        patch("app.services.memory_context.get_recent_fragments_by_grain",
              new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=[],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao",
            chat_name="测试群",
        )

    assert "群聊「测试群」" in result
    assert "今天的基调" not in result
    assert "脑子里的东西" not in result
    assert "更远的记忆" not in result
    assert "recall" in result


@pytest.mark.asyncio
async def test_build_inner_context_proactive():
    """主动发言：含 stimulus，无回复提示"""
    with (
        patch("app.services.memory_context._build_today_state",
              new_callable=AsyncMock, return_value=""),
        patch("app.services.memory_context.get_today_fragments",
              new_callable=AsyncMock, return_value=[]),
        patch("app.services.memory_context.get_recent_fragments_by_grain",
              new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥", persona_id="akao",
            chat_name="摸鱼群", is_proactive=True,
            proactive_stimulus="有人在讨论猫猫",
        )

    assert "摸鱼群" in result
    assert "刷到了群里的对话" in result
    assert "猫猫" in result
    assert "回复" not in result


# ── get_reply_style tests (unchanged) ──


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
