import pytest
from unittest.mock import AsyncMock, patch

from app.services.message_router import MessageRouter


@pytest.fixture
def router():
    return MessageRouter()


@pytest.mark.asyncio
async def test_p2p_routes_to_bot_persona(router):
    """P2P 消息用 bot_name 反查 persona_id"""
    with patch(
        "app.services.message_router._resolve_persona_id",
        new_callable=AsyncMock,
        return_value="akao",
    ):
        result = await router.route(
            chat_id="c1", mentions=[], bot_name="fly", is_p2p=True
        )
    assert result == ["akao"]


@pytest.mark.asyncio
async def test_group_with_mention_routes_to_mentioned_personas(router):
    """群聊 @ 了已注册 bot → 返回对应 persona_id 列表"""
    with patch(
        "app.services.message_router.resolve_mentioned_personas",
        new_callable=AsyncMock,
        return_value=["akao", "chinagi"],
    ):
        result = await router.route(
            chat_id="c1",
            mentions=["union_bot_fly", "union_bot_chinagi"],
            bot_name="fly",
            is_p2p=False,
        )
    assert result == ["akao", "chinagi"]


@pytest.mark.asyncio
async def test_group_with_mention_no_match_returns_empty(router):
    """群聊 @ 了非 bot 用户 → 返回空列表"""
    with patch(
        "app.services.message_router.resolve_mentioned_personas",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await router.route(
            chat_id="c1",
            mentions=["union_some_user"],
            bot_name="fly",
            is_p2p=False,
        )
    assert result == []


@pytest.mark.asyncio
async def test_group_no_mention_returns_empty(router):
    """群聊无 @ → 不回复"""
    result = await router.route(
        chat_id="c1", mentions=[], bot_name="fly", is_p2p=False
    )
    assert result == []
