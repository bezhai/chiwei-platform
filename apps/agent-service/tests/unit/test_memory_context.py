"""测试统一聊天注入上下文 v4（Life Engine 版）"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── build_inner_context tests ──


@pytest.mark.asyncio
async def test_build_inner_context_group_basic():
    """群聊：场景提示 + Life Engine 状态"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="你此刻的状态：窝在被窝里\n你的心情：暖洋洋的",
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="A哥",
            persona_id="akao",
            chat_name="KA技术群",
        )

    assert "群聊「KA技术群」" in result
    assert "回复 A哥" in result
    assert "窝在被窝里" in result
    assert "暖洋洋" in result


@pytest.mark.asyncio
async def test_build_inner_context_p2p():
    """私聊：显示私聊场景"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="",
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="p2p_001",
            chat_type="p2p",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="A哥",
            persona_id="akao",
        )

    assert "私聊" in result


@pytest.mark.asyncio
async def test_build_inner_context_no_life_state():
    """无 Life Engine 状态时：只有场景提示，不崩溃"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="",
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=[],
            trigger_user_id="u1",
            trigger_username="A哥",
            persona_id="akao",
            chat_name="测试群",
        )

    assert "群聊「测试群」" in result
    assert "此刻的状态" not in result


@pytest.mark.asyncio
async def test_build_inner_context_proactive():
    """主动发言：含 stimulus，无回复提示"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="",
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="A哥",
            persona_id="akao",
            chat_name="摸鱼群",
            is_proactive=True,
            proactive_stimulus="有人在讨论猫猫",
        )

    assert "摸鱼群" in result
    assert "刷到了群里的对话" in result
    assert "猫猫" in result
    assert "回复" not in result


# ── get_reply_style tests ──


@pytest.mark.asyncio
async def test_get_reply_style_from_db():
    """DB 有记录 → 返回 DB 值"""
    with patch(
        "app.orm.memory_crud.get_latest_reply_style",
        new_callable=AsyncMock,
        return_value="[感冒中] 说话短短的",
    ):
        from app.services.memory_context import get_reply_style

        result = await get_reply_style("akao")

    assert "感冒" in result


@pytest.mark.asyncio
async def test_get_reply_style_fallback_to_default():
    """DB 无记录 → fallback 到 default_style"""
    with patch(
        "app.orm.memory_crud.get_latest_reply_style",
        new_callable=AsyncMock,
        return_value=None,
    ):
        from app.services.memory_context import get_reply_style

        result = await get_reply_style("akao", default_style="来自persona的默认风格")

    assert "来自persona的默认风格" in result


@pytest.mark.asyncio
async def test_get_reply_style_empty_default():
    """DB 无记录 + 无 default → 返回空字符串"""
    with patch(
        "app.orm.memory_crud.get_latest_reply_style",
        new_callable=AsyncMock,
        return_value=None,
    ):
        from app.services.memory_context import get_reply_style

        result = await get_reply_style("akao")

    assert result == ""


# ── Life Engine state injection tests ──


@pytest.mark.asyncio
async def test_inner_context_includes_life_engine_state():
    """build_inner_context 注入 Life Engine 状态"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="你此刻的状态：窝在被窝里刷手机\n你的心情：暖洋洋的，很放松",
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="oc_test",
            chat_type="group",
            user_ids=[],
            trigger_user_id="u1",
            trigger_username="测试",
            persona_id="akao-001",
            chat_name="测试群",
        )
        assert "窝在被窝里刷手机" in result
        assert "暖洋洋" in result


@pytest.mark.asyncio
async def test_inner_context_no_life_state_graceful():
    """DB 无 Life Engine 状态 → 不崩溃，不注入"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="",
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="oc_test",
            chat_type="p2p",
            user_ids=[],
            trigger_user_id="u1",
            trigger_username="测试",
            persona_id="akao-001",
        )
        assert isinstance(result, str)
        assert "窝在被窝里" not in result


@pytest.mark.asyncio
async def test_relationship_memory_injection_core_facts_and_impression():
    """关系记忆应以 [事实] + [印象] 格式注入"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="",
    ), patch(
        "app.orm.memory_crud.get_latest_relationship_memory",
        new_callable=AsyncMock,
        return_value=("群昵称 crgg，经常被泼洗脚水", "脑回路清奇但偶尔挺好笑"),
    ), patch(
        "app.services.memory_context.get_today_fragments",
        new_callable=AsyncMock,
        return_value=[],
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="crgg",
            persona_id="chiwei",
            chat_name="KA群",
        )

    assert "关于 crgg" in result
    assert "[事实] 群昵称 crgg" in result
    assert "[印象] 脑回路清奇" in result


@pytest.mark.asyncio
async def test_relationship_memory_injection_no_memory():
    """无关系记忆时不注入"""
    with patch(
        "app.services.memory_context._build_life_state",
        new_callable=AsyncMock,
        return_value="",
    ), patch(
        "app.orm.memory_crud.get_latest_relationship_memory",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "app.services.memory_context.get_today_fragments",
        new_callable=AsyncMock,
        return_value=[],
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="crgg",
            persona_id="chiwei",
            chat_name="KA群",
        )

    assert "[事实]" not in result
    assert "[印象]" not in result
