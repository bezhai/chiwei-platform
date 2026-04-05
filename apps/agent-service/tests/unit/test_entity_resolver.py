# tests/unit/test_entity_resolver.py
"""测试实体解析器的各项功能"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# format_entity_ref
# ---------------------------------------------------------------------------


def test_format_entity_ref_with_name():
    """有 display_name 时返回 '名字(#id)'"""
    entity = MagicMock()
    entity.id = 3
    entity.display_name = "阿儒"

    from app.services.entity_resolver import format_entity_ref
    assert format_entity_ref(entity) == "阿儒(#3)"


def test_format_entity_ref_without_name():
    """没有 display_name 时返回 '#id'"""
    entity = MagicMock()
    entity.id = 7
    entity.display_name = None

    from app.services.entity_resolver import format_entity_ref
    assert format_entity_ref(entity) == "#7"


def test_format_entity_ref_empty_string_name():
    """display_name 为空字符串时视同无名，返回 '#id'"""
    entity = MagicMock()
    entity.id = 5
    entity.display_name = ""

    from app.services.entity_resolver import format_entity_ref
    assert format_entity_ref(entity) == "#5"


# ---------------------------------------------------------------------------
# _get_chat_display_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_chat_display_name_p2p_returns_none():
    """p2p 聊天不查数据库，直接返回 None"""
    from app.services.entity_resolver import _get_chat_display_name
    result = await _get_chat_display_name("chat_123", "p2p")
    assert result is None


@pytest.mark.asyncio
async def test_get_chat_display_name_group_found():
    """group 类型查到群名时返回名字"""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "番剧群"
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.services.entity_resolver.AsyncSessionLocal", return_value=mock_session):
        from app.services.entity_resolver import _get_chat_display_name
        result = await _get_chat_display_name("chat_abc", "group")

    assert result == "番剧群"


@pytest.mark.asyncio
async def test_get_chat_display_name_group_not_found():
    """group 类型查不到群名时返回 None"""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.services.entity_resolver.AsyncSessionLocal", return_value=mock_session):
        from app.services.entity_resolver import _get_chat_display_name
        result = await _get_chat_display_name("chat_abc", "group")

    assert result is None


# ---------------------------------------------------------------------------
# resolve_participants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_participants_empty():
    """空 user_ids 直接返回空 dict，不调用任何 DB"""
    with patch("app.services.entity_resolver.get_username") as mock_username, \
         patch("app.services.entity_resolver.batch_get_or_create_entities") as mock_batch:
        from app.services.entity_resolver import resolve_participants
        result = await resolve_participants([])

    assert result == {}
    mock_username.assert_not_called()
    mock_batch.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_participants_single_user():
    """单个 user_id，从 lark_user 取名字后 batch upsert"""
    entity = MagicMock()
    entity.display_name = "阿儒"

    with patch("app.services.entity_resolver.get_username", new=AsyncMock(return_value="阿儒")), \
         patch(
             "app.services.entity_resolver.batch_get_or_create_entities",
             new=AsyncMock(return_value={"uid_1": entity}),
         ):
        from app.services.entity_resolver import resolve_participants
        result = await resolve_participants(["uid_1"])

    assert result == {"uid_1": entity}


@pytest.mark.asyncio
async def test_resolve_participants_user_not_in_lark():
    """lark_user 查不到时 display_name 为 None"""
    entity = MagicMock()
    entity.display_name = None

    with patch("app.services.entity_resolver.get_username", new=AsyncMock(return_value=None)), \
         patch(
             "app.services.entity_resolver.batch_get_or_create_entities",
             new=AsyncMock(return_value={"uid_unknown": entity}),
         ) as mock_batch:
        from app.services.entity_resolver import resolve_participants
        await resolve_participants(["uid_unknown"])

    # 确认传入了 display_name=None
    call_args = mock_batch.call_args[0][0]
    assert call_args == [("user", "uid_unknown", None)]


# ---------------------------------------------------------------------------
# resolve_chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_chat_group():
    """group 类型 → entity_type='group'"""
    entity = MagicMock()

    with patch(
        "app.services.entity_resolver._get_chat_display_name",
        new=AsyncMock(return_value="番剧群"),
    ), patch(
        "app.services.entity_resolver.get_or_create_entity",
        new=AsyncMock(return_value=entity),
    ) as mock_upsert:
        from app.services.entity_resolver import resolve_chat
        result = await resolve_chat("chat_abc", "group")

    mock_upsert.assert_awaited_once_with("group", "chat_abc", "番剧群")
    assert result is entity


@pytest.mark.asyncio
async def test_resolve_chat_p2p():
    """p2p 类型 → entity_type='p2p'，display_name=None"""
    entity = MagicMock()

    with patch(
        "app.services.entity_resolver._get_chat_display_name",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.entity_resolver.get_or_create_entity",
        new=AsyncMock(return_value=entity),
    ) as mock_upsert:
        from app.services.entity_resolver import resolve_chat
        result = await resolve_chat("chat_p2p", "p2p")

    mock_upsert.assert_awaited_once_with("p2p", "chat_p2p", None)
    assert result is entity


# ---------------------------------------------------------------------------
# build_entity_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_entity_context_basic():
    """build_entity_context 返回 (name_map, mentioned_ids)"""
    user_entity = MagicMock()
    user_entity.id = 3
    user_entity.display_name = "阿儒"

    chat_entity = MagicMock()
    chat_entity.id = 7
    chat_entity.display_name = "番剧群"

    with patch(
        "app.services.entity_resolver.resolve_participants",
        new=AsyncMock(return_value={"uid_1": user_entity}),
    ), patch(
        "app.services.entity_resolver.resolve_chat",
        new=AsyncMock(return_value=chat_entity),
    ):
        from app.services.entity_resolver import build_entity_context
        name_map, mentioned_ids = await build_entity_context(
            ["uid_1"], "chat_abc", "group"
        )

    assert name_map == {"uid_1": "阿儒(#3)", "chat_abc": "番剧群(#7)"}
    assert mentioned_ids[0] == 7  # chat entity first
    assert 3 in mentioned_ids


@pytest.mark.asyncio
async def test_build_entity_context_no_users():
    """没有 user_ids 时，name_map 为空，mentioned_ids 只含 chat"""
    chat_entity = MagicMock()
    chat_entity.id = 7
    chat_entity.display_name = "番剧群"

    with patch(
        "app.services.entity_resolver.resolve_participants",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.services.entity_resolver.resolve_chat",
        new=AsyncMock(return_value=chat_entity),
    ):
        from app.services.entity_resolver import build_entity_context
        name_map, mentioned_ids = await build_entity_context([], "chat_abc", "group")

    assert name_map == {"chat_abc": "番剧群(#7)"}
    assert mentioned_ids == [7]


@pytest.mark.asyncio
async def test_build_entity_context_multiple_users():
    """多个 user_ids，name_map 和 mentioned_ids 都包含所有用户"""
    user1 = MagicMock()
    user1.id = 1
    user1.display_name = "阿儒"

    user2 = MagicMock()
    user2.id = 2
    user2.display_name = None  # 没有名字

    chat_entity = MagicMock()
    chat_entity.id = 10
    chat_entity.display_name = "测试群"

    with patch(
        "app.services.entity_resolver.resolve_participants",
        new=AsyncMock(return_value={"uid_1": user1, "uid_2": user2}),
    ), patch(
        "app.services.entity_resolver.resolve_chat",
        new=AsyncMock(return_value=chat_entity),
    ):
        from app.services.entity_resolver import build_entity_context
        name_map, mentioned_ids = await build_entity_context(
            ["uid_1", "uid_2"], "chat_abc", "group"
        )

    assert name_map["uid_1"] == "阿儒(#1)"
    assert name_map["uid_2"] == "#2"
    assert set(mentioned_ids) == {1, 2, 10}
