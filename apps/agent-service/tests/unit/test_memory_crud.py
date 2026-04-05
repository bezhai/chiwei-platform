# tests/unit/test_memory_crud.py
"""测试 memory CRUD 函数的参数传递、SQL 构建和返回值"""
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_session():
    """构造带 context manager 支持的 mock session"""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _make_mock_result(scalar_value=None, scalars_all=None):
    """构造 execute() 返回的 mock result"""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = scalar_value
    if scalars_all is not None:
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = scalars_all
        mock_result.scalars.return_value = mock_scalars
    return mock_result


# ---------------------------------------------------------------------------
# create_fragment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_fragment_adds_and_commits():
    """create_fragment 应调用 add + commit + refresh 并返回碎片"""
    from app.orm.memory_models import ExperienceFragment

    mock_session = _make_mock_session()
    fragment = ExperienceFragment(
        persona_id="akao",
        grain="conversation",
        content="今天和阿儒聊了新番",
    )

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import create_fragment
        result = await create_fragment(fragment)

    mock_session.add.assert_called_once_with(fragment)
    mock_session.commit.assert_awaited_once()
    mock_session.refresh.assert_awaited_once_with(fragment)
    assert result is fragment


# ---------------------------------------------------------------------------
# get_fragments_for_chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_fragments_for_chat_no_grain_filter():
    """get_fragments_for_chat 不传 grains 时应返回 execute 结果"""
    from app.orm.memory_models import ExperienceFragment

    frag = MagicMock(spec=ExperienceFragment)
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalars_all=[frag]))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_fragments_for_chat
        result = await get_fragments_for_chat("akao", "chat_abc")

    mock_session.execute.assert_awaited_once()
    assert result == [frag]


@pytest.mark.asyncio
async def test_get_fragments_for_chat_with_grain_filter():
    """传入 grains 参数时也正常执行"""
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalars_all=[]))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_fragments_for_chat
        result = await get_fragments_for_chat("akao", "chat_abc", grains=["conversation"])

    assert result == []


# ---------------------------------------------------------------------------
# get_recent_fragments_by_grain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_fragments_by_grain_returns_list():
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalars_all=[]))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_recent_fragments_by_grain
        result = await get_recent_fragments_by_grain("akao", "daily", limit=5)

    assert result == []
    mock_session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_today_fragments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_today_fragments_no_filters():
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalars_all=[]))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_today_fragments
        result = await get_today_fragments("akao")

    assert result == []


@pytest.mark.asyncio
async def test_get_today_fragments_with_chat_id():
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalars_all=[]))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_today_fragments
        result = await get_today_fragments("akao", source_chat_id="chat_xyz")

    assert result == []


# ---------------------------------------------------------------------------
# get_fragments_in_date_range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_fragments_in_date_range():
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalars_all=[]))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_fragments_in_date_range
        result = await get_fragments_in_date_range(
            "akao",
            date(2026, 4, 1),
            date(2026, 4, 7),
        )

    assert result == []
    mock_session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# search_fragments_fts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_fragments_fts_returns_list():
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalars_all=[]))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import search_fragments_fts
        result = await search_fragments_fts("akao", "新番")

    assert result == []
    mock_session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_or_create_entity — 已存在
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_entity_existing_no_name_change():
    """实体已存在且 display_name 未变化 → 直接返回，不 commit"""
    existing = MagicMock()
    existing.display_name = "阿儒"

    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalar_value=existing))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_or_create_entity
        result = await get_or_create_entity("user", "uid_123", "阿儒")

    mock_session.commit.assert_not_awaited()
    assert result is existing


@pytest.mark.asyncio
async def test_get_or_create_entity_existing_name_changed():
    """实体已存在但 display_name 变了 → 更新并 commit"""
    existing = MagicMock()
    existing.display_name = "旧名字"

    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalar_value=existing))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_or_create_entity
        result = await get_or_create_entity("user", "uid_123", "新名字")

    assert existing.display_name == "新名字"
    mock_session.commit.assert_awaited_once()
    assert result is existing


@pytest.mark.asyncio
async def test_get_or_create_entity_not_exists_creates():
    """实体不存在 → 创建并返回"""
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock(return_value=_make_mock_result(scalar_value=None))

    with patch("app.orm.memory_crud.AsyncSessionLocal", return_value=mock_session):
        from app.orm.memory_crud import get_or_create_entity
        result = await get_or_create_entity("group", "chat_abc", "番剧群")

    mock_session.add.assert_called_once()
    mock_session.commit.assert_awaited_once()
    mock_session.refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# batch_get_or_create_entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_get_or_create_entities_returns_map():
    """批量 upsert 返回 {external_id: entity} 映射"""
    entity1 = MagicMock()
    entity1.display_name = "阿儒"
    entity2 = MagicMock()
    entity2.display_name = "番剧群"

    call_count = 0

    async def fake_get_or_create(entity_type, external_id, display_name=None):
        nonlocal call_count
        call_count += 1
        if external_id == "uid_1":
            return entity1
        return entity2

    with patch("app.orm.memory_crud.get_or_create_entity", side_effect=fake_get_or_create):
        from app.orm.memory_crud import batch_get_or_create_entities
        result = await batch_get_or_create_entities([
            ("user", "uid_1", "阿儒"),
            ("group", "chat_abc", "番剧群"),
        ])

    assert call_count == 2
    assert result["uid_1"] is entity1
    assert result["chat_abc"] is entity2


@pytest.mark.asyncio
async def test_batch_get_or_create_entities_empty():
    """空列表返回空 dict"""
    with patch("app.orm.memory_crud.get_or_create_entity"):
        from app.orm.memory_crud import batch_get_or_create_entities
        result = await batch_get_or_create_entities([])

    assert result == {}
